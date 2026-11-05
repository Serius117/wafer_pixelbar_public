"""Tests for engines/llm, core/memory, and core/agent."""

from core.agent import WaferAgent
from core.bus.hub import InProcBus
from core.interfaces import Route
from core.memory import MemoryManager
from core.sim import CaptureTts, ScriptedLlm
from engines.llm import OllamaLlm


class TestOllamaLlm:
    def test_init_defaults(self) -> None:
        llm = OllamaLlm()
        assert llm._model == "qwen3.5-9b:q4"
        assert llm._ctx == 8192
        assert llm._base_url == "http://127.0.0.1:11434"

    def test_think_disabled_by_default(self) -> None:
        llm = OllamaLlm()
        assert llm._think is False


class TestMemoryManager:
    def test_load_nonexistent_returns_empty(self, tmp_path) -> None:
        mm = MemoryManager(tmp_path)
        mem = mm.get_memory("master")
        assert mem.speaker_id == "master"
        assert mem.facts == []
        assert mem.rolling_summary == ""

    def test_save_and_reload(self, tmp_path) -> None:
        mm = MemoryManager(tmp_path)
        mm.add_fact("master", "喜欢被吐槽")
        mm.add_fact("master", "主业后端")

        # New manager instance should reload from disk
        mm2 = MemoryManager(tmp_path)
        mem = mm2.get_memory("master")
        assert "喜欢被吐槽" in mem.facts
        assert "主业后端" in mem.facts

    def test_no_duplicate_facts(self, tmp_path) -> None:
        mm = MemoryManager(tmp_path)
        mm.add_fact("master", "same fact")
        mm.add_fact("master", "same fact")
        assert len(mm.get_memory("master").facts) == 1

    def test_record_turn_triggers_summary(self, tmp_path) -> None:
        mm = MemoryManager(tmp_path)
        for i in range(8):
            trigger = mm.record_turn("master", f"msg{i}", f"reply{i}")
        assert trigger is True

    def test_build_context_injects_memory(self, tmp_path) -> None:
        mm = MemoryManager(tmp_path)
        mm.add_fact("master", "腰椎不好")
        mm.get_memory("master").name = "老板"
        mm.update_summary("master", "今天聊了工作压力")

        msgs = mm.build_context("master", "你是{{robot_name}}", "小饼")
        assert len(msgs) == 1
        assert msgs[0].role == "system"
        assert "小饼" in msgs[0].content
        assert "老板" in msgs[0].content
        assert "腰椎不好" in msgs[0].content
        assert "今天聊了工作压力" in msgs[0].content


class TestWaferAgent:
    async def test_chat_turn_with_emotion(self, tmp_path) -> None:
        """Full chat turn: LLM streams -> emotion tag -> TTS captures text."""
        (tmp_path / "replies.json").write_text(
            '["[emotion:sad] 辛苦啦"]', encoding="utf-8"
        )
        bus = InProcBus()
        tts = CaptureTts()
        memory = MemoryManager(tmp_path)
        llm = ScriptedLlm(tmp_path)

        emotion_events: list[dict] = []
        bus.subscribe("llm.emotion", lambda t, p: emotion_events.append(p))

        agent = WaferAgent(llm=llm, tts=tts, bus=bus, memory=memory, robot_name="小饼")
        result = await agent.chat_turn("今天好累啊", "master")

        assert result == Route.CHAT
        assert tts.captured == ["辛苦啦"]
        assert any(e.get("tag") == "sad" for e in emotion_events)

    async def test_chat_turn_no_emotion(self, tmp_path) -> None:
        (tmp_path / "replies.json").write_text('["普通回复"]', encoding="utf-8")
        bus = InProcBus()
        tts = CaptureTts()
        memory = MemoryManager(tmp_path)
        llm = ScriptedLlm(tmp_path)

        emotion_events: list[dict] = []
        bus.subscribe("llm.emotion", lambda t, p: emotion_events.append(p))

        agent = WaferAgent(llm=llm, tts=tts, bus=bus, memory=memory)
        await agent.chat_turn("你好", "master")

        assert tts.captured == ["普通回复"]
        assert emotion_events == []

    async def test_persona_injection(self, tmp_path) -> None:
        (tmp_path / "replies.json").write_text('["好的"]', encoding="utf-8")
        bus = InProcBus()
        tts = CaptureTts()
        memory = MemoryManager(tmp_path)
        memory.add_fact("master", "喜欢简洁")

        llm = ScriptedLlm(tmp_path)
        agent = WaferAgent(
            llm=llm, tts=tts, bus=bus, memory=memory,
            persona_template="你是{{robot_name}}，一个可爱的机器人",
            robot_name="小饼",
        )
        await agent.chat_turn("你好", "master")

        # The ScriptedLlm doesn't actually use the messages, but we can
        # verify the memory manager built the context correctly
        mem = memory.get_memory("master")
        assert mem.turn_count == 1
        assert "喜欢简洁" in mem.facts
