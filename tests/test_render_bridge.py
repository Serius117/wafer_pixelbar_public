"""W.6: RenderEventBridge -- bus events drive the face.

Before W.6 the pipeline published emotion/state/amplitude/command events and
nothing consumed them (the face never moved). These tests lock the wiring.
"""

import time

from core.bus import topics
from core.bus.hub import InProcBus
from render.claw_face import ClawFace
from render.event_bridge import COMMAND_EXPRESSIONS, SCENE_EXPRESSIONS, RenderEventBridge


async def make_bridge() -> tuple[RenderEventBridge, ClawFace, InProcBus]:
    bus = InProcBus()
    face = ClawFace()
    bridge = RenderEventBridge(bus, face)
    await bridge.start()
    return bridge, face, bus


async def pub(bus: InProcBus, topic: str, payload: dict) -> None:
    await bus.publish(topic, payload)


class TestSignalWiring:
    async def test_session_state_drives_face(self) -> None:
        bridge, face, bus = await make_bridge()
        await pub(bus, topics.SESSION_STATE, {"state": "standby"})
        assert face.state.expression == "sleepy"

        await pub(bus, topics.SESSION_STATE, {"state": "listening"})
        assert face.state.is_listening is True

    async def test_emotion_drives_expression(self) -> None:
        bridge, face, bus = await make_bridge()
        await pub(bus, topics.LLM_EMOTION, {"tag": "sad"})
        assert face.state.expression == "sad"

    async def test_amplitude_drives_mouth(self) -> None:
        bridge, face, bus = await make_bridge()
        await pub(bus, topics.TTS_AMPLITUDE, {"rms": 0.7})
        assert face.state.mouth_open == 0.7

    async def test_emotion_ignored_during_override(self) -> None:
        """An active command overlay wins over concurrent LLM emotions."""
        bridge, face, bus = await make_bridge()
        await pub(bus, topics.RENDER_COMMAND, {"cmd": "question", "duration_ms": 60000})
        await pub(bus, topics.LLM_EMOTION, {"tag": "happy"})

        assert face.state.expression == "confused"  # overlay held
        assert bridge.override_active is True


class TestCommandOverlay:
    async def test_question_command(self) -> None:
        bridge, face, bus = await make_bridge()
        await pub(bus, topics.RENDER_COMMAND, {"cmd": "question", "duration_ms": 3000})

        assert face.state.expression == "confused"
        assert face.state.status_text == "?"
        assert bridge.override_active is True

    async def test_error_command(self) -> None:
        bridge, face, bus = await make_bridge()
        await pub(bus, topics.RENDER_COMMAND, {"cmd": "error", "duration_ms": 3000})
        assert face.state.expression == "scared"

    async def test_overlay_expires_and_restores_emotion(self) -> None:
        bridge, face, bus = await make_bridge()
        # establish a pre-existing emotion, then overlay, then expire
        await pub(bus, topics.LLM_EMOTION, {"tag": "happy"})
        await pub(bus, topics.RENDER_COMMAND, {"cmd": "question", "duration_ms": 50})
        assert face.state.expression == "confused"

        time.sleep(0.08)
        bridge.apply_timeouts()

        assert bridge.override_active is False
        assert face.state.expression == "happy"  # restored

    async def test_overlay_expiry_falls_back_to_normal(self) -> None:
        """No prior emotion -> expiry restores normal."""
        bridge, face, bus = await make_bridge()
        await pub(bus, topics.RENDER_COMMAND, {"cmd": "error", "duration_ms": 50})

        time.sleep(0.08)
        bridge.apply_timeouts()
        assert face.state.expression == "normal"


class TestSceneSwitch:
    async def test_game_hud_scene(self) -> None:
        bridge, face, bus = await make_bridge()
        await pub(bus, topics.RENDER_COMMAND, {"cmd": "scene", "scene": "game_hud"})

        assert bridge.scene == "game_hud"
        assert face.state.expression == "typing"
        assert face.state.status_text == "GAME"

    async def test_idle_scene_restores(self) -> None:
        bridge, face, bus = await make_bridge()
        await pub(bus, topics.RENDER_COMMAND, {"cmd": "scene", "scene": "game_hud"})
        await pub(bus, topics.RENDER_COMMAND, {"cmd": "scene", "scene": "idle"})

        assert bridge.scene == "idle"
        assert face.state.expression == "normal"

    async def test_scene_blocks_emotion(self) -> None:
        """In game HUD scene, LLM emotions must not change the face."""
        bridge, face, bus = await make_bridge()
        await pub(bus, topics.RENDER_COMMAND, {"cmd": "scene", "scene": "game_hud"})
        await pub(bus, topics.LLM_EMOTION, {"tag": "sad"})

        assert face.state.expression == "typing"

    async def test_scene_restored_after_overlay(self) -> None:
        """Overlay inside a scene expires back to the scene expression."""
        bridge, face, bus = await make_bridge()
        await pub(bus, topics.RENDER_COMMAND, {"cmd": "scene", "scene": "game_hud"})
        await pub(bus, topics.RENDER_COMMAND, {"cmd": "error", "duration_ms": 50})
        assert face.state.expression == "scared"

        time.sleep(0.08)
        bridge.apply_timeouts()
        assert face.state.expression == "typing"  # back to scene


class TestCommandMaps:
    def test_known_commands_mapped(self) -> None:
        assert set(COMMAND_EXPRESSIONS) == {"question", "error"}
        assert set(SCENE_EXPRESSIONS) == {"game_hud", "idle"}

    async def test_unknown_command_no_crash(self) -> None:
        bridge, face, bus = await make_bridge()
        await pub(bus, topics.RENDER_COMMAND, {"cmd": "wat"})
        assert face.state.expression == "normal"
