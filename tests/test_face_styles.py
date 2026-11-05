"""F group tests: 4 face styles + FaceManager switching."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestWaferFace:
    """F.5: wafer expression set — clean, minimal, user-named."""

    def _face(self):
        from render.faces.wafer import WaferFace
        return WaferFace()

    def test_render_normal(self) -> None:
        face = self._face()
        face.tick(0.033)
        frame = face.render()
        assert frame.width == 256 and frame.height == 32
        assert any(frame.pixels[i] > 0 for i in range(0, len(frame.pixels), 3))

    def test_happy_mouth_is_pure_arc(self) -> None:
        """The 'drooling' fix: happy mouth = U-shape arc ONLY (smile).
        Middle dips DOWN (larger y), corners at top — no base line."""
        face = self._face()
        face.set_expression("happy")
        face.tick(0.033)
        frame = face.render()
        mouth_y = 25  # MOUTH_CY
        cx = 128
        # Check the arc middle is BELOW MOUTH_CY (smile = U shape)
        # and there's no solid line AT MOUTH_CY
        line_pixels = sum(1 for x in range(cx - 11, cx + 12)
                          if frame.pixels[(mouth_y * 256 + x) * 3] > 10)
        assert line_pixels == 0, f"base has {line_pixels}px (want 0 for pure U arc)"

    def test_happy_eyes_are_curved_arcs(self) -> None:
        """2026-10-24 redesign: happy eyes are ∩ arcs (classic curved
        eyes), not filled dot circles. At each eye's center column only
        2-3 rows are lit (the arc cross-section); a dot eye would light
        ~9-11 rows (filled circle + glow)."""
        face = self._face()
        face.set_expression("happy")
        face.tick(0.033)
        frame = face.render()
        for ecx in (128 - 36, 128 + 36):  # SPREAD = 36
            lit_rows = sum(1 for y in range(0, 20)
                           if frame.pixels[(y * 256 + ecx) * 3] > 10)
            assert 1 <= lit_rows <= 4, (
                f"eye column x={ecx}: {lit_rows} lit rows "
                f"(want 2-3 for arc, not a filled dot)")

    def test_sad_mouth_is_pure_arc(self) -> None:
        """Sad mouth = ∩-shape arc ONLY (frown). Middle bumps UP."""
        face = self._face()
        face.set_expression("sad")
        face.tick(0.033)
        frame = face.render()
        mouth_y = 25
        cx = 128
        line_pixels = sum(1 for x in range(cx - 11, cx + 12)
                          if frame.pixels[(mouth_y * 256 + x) * 3] > 10)
        assert line_pixels == 0

    def test_angry_chevron_eyes_and_wavy_mouth(self) -> None:
        """2026-10-24 redesign: angry = ＞＜ shut eyes (inward chevrons,
        NO separate brows) + pursed-lips wavy mouth. Wavy mouth
        oscillates: pixels above AND below MOUTH_CY (the old flat
        thick line never rose above it)."""
        face = self._face()
        face.set_expression("angry")
        face.tick(0.033)
        frame = face.render()
        # eye boxes: chevron arms light far less than a filled dot+glow
        for ecx in (128 - 36, 128 + 36):
            lit = sum(1 for y in range(2, 22) for x in range(ecx - 10, ecx + 11)
                      if frame.pixels[(y * 256 + x) * 3] > 10)
            assert 15 < lit < 120, (
                f"eye box x~{ecx}: {lit} px (chevron arms ~60)")
        # wavy mouth: must cross MOUTH_CY (25) in both directions
        above = any(frame.pixels[(y * 256 + x) * 3] > 10
                    for y in range(22, 25) for x in range(117, 140))
        below = any(frame.pixels[(y * 256 + x) * 3] > 10
                    for y in range(26, 29) for x in range(117, 140))
        assert above and below, "wavy mouth must oscillate around MOUTH_CY"

    def test_surprised_hollow_circle(self) -> None:
        face = self._face()
        face.set_expression("surprised")
        face.tick(0.033)
        frame = face.render()
        # The circle should have a hole (not solid)
        mouth_y = 25
        cx = 128
        # Center of circle should be dark (hollow)
        center_idx = (mouth_y * 256 + cx) * 3
        assert frame.pixels[center_idx] < 20, "mouth center not hollow"

    def test_all_expressions_render(self) -> None:
        face = self._face()
        for expr in ("normal", "happy", "sad", "surprised", "angry",
                     "sleepy", "wink", "cute", "scared", "tired",
                     "thinking", "celebrate"):
            face.set_expression(expr)
            face.tick(0.033)
            frame = face.render()
            assert frame.width == 256
            assert len(frame.pixels) == 256 * 32 * 3

    def test_celebrate_star_eyes_not_dots(self) -> None:
        """v1 (2026-10-24, §4.5.3.2 slot 7): celebrate = curved-diamond
        sparkle eyes (filled astroid, concave edges). Filled astroid
        lights ~42-75px per eye box; a filled dot eye (radius ~7 +
        glow) would light ~350px -- clear separation."""
        face = self._face()
        face.set_expression("celebrate")
        face.tick(0.033)
        frame = face.render()
        for ecx in (128 - 36, 128 + 36):  # SPREAD = 36
            lit = sum(1 for y in range(2, 22) for x in range(ecx - 10, ecx + 11)
                      if frame.pixels[(y * 256 + x) * 3] > 10)
            assert 20 < lit < 120, (
                f"eye box x~{ecx}: {lit} px "
                f"(star bars ~50, filled dot ~350)")

    def test_thinking_monocle_fixed_and_no_hand(self) -> None:
        """v2 (2026-10-24, user feedback): monocle ring is FIXED --
        never shrinks with blink (glasses don't blink); hand removed
        (awkward) -- mouth only. Also: ring adds pixels to the right
        eye box; the mouth line sits LEFT of center."""
        face = self._face()
        face.set_expression("thinking")
        face.tick(0.033)
        frame = face.render()

        def box_lit(x0, x1, y0, y1):
            return sum(1 for y in range(y0, y1) for x in range(x0, x1)
                       if frame.pixels[(y * 256 + x) * 3] > 10)

        left = box_lit(82, 103, 2, 22)    # left eye box
        right = box_lit(153, 174, 2, 22)  # right eye box (monocle)
        assert right > left + 15, (
            f"monocle ring missing: left={left}, right={right}")
        # hand removed: the old fist/wrist band must be empty
        assert box_lit(130, 162, 26, 32) == 0, "hand should be removed"
        # mouth shifted LEFT of center
        assert box_lit(112, 124, 24, 26) > 0, "mouth not left-shifted"
        assert box_lit(132, 145, 24, 26) == 0

        # monocle survives blink: force blink; the ring's TOPMOST row
        # must stay put (the old bug scaled ring_r with the blink-
        # shrunken eye radius -> top row jumped from y=3 to y=7)
        face.state._blinking = True
        frame2 = face.render()

        def ring_top(img):
            ys = [y for y in range(0, 22) for x in range(150, 180)
                  if img.pixels[(y * 256 + x) * 3] > 10]
            return min(ys) if ys else 99

        assert ring_top(frame) == ring_top(frame2) < 6, (
            f"monocle ring moved/shrunk during blink: "
            f"normal top={ring_top(frame)}, blink top={ring_top(frame2)}")

    def test_expression_changes_pixels(self) -> None:
        face = self._face()
        face.set_expression("normal")
        face.tick(0.033)
        f1 = face.render()
        face.set_expression("surprised")
        face.tick(0.033)
        f2 = face.render()
        assert f1.pixels != f2.pixels

    def test_sleepy_has_no_mouth(self) -> None:
        face = self._face()
        face.set_expression("sleepy")
        face.tick(0.033)
        frame = face.render()
        # Check mouth area (y=25) has no lit pixels in center region
        mouth_y = 25
        cx = 128
        mouth_pixels = sum(1 for x in range(cx - 15, cx + 16)
                           if frame.pixels[(mouth_y * 256 + x) * 3] > 10)
        assert mouth_pixels == 0, "sleepy should have no mouth"


class TestFaceManager:
    def test_default_wafer(self) -> None:
        from render.faces.manager import FaceManager
        fm = FaceManager()
        assert fm.style == "wafer"

    def test_switch_all_styles(self) -> None:
        from render.faces.manager import FaceManager
        fm = FaceManager()
        for style in ("mochi", "bubble", "mech", "claw", "wafer"):
            assert fm.switch(style) is True
            assert fm.style == style

    def test_switch_unknown_fails(self) -> None:
        from render.faces.manager import FaceManager
        fm = FaceManager()
        assert fm.switch("nonexistent") is False
        assert fm.style == "wafer"

    def test_same_style_noop(self) -> None:
        from render.faces.manager import FaceManager
        fm = FaceManager()
        assert fm.switch("claw") is True  # already active
        assert fm.style == "claw"

    def test_available_styles(self) -> None:
        from render.faces.manager import STYLES
        assert set(STYLES) == {"wafer", "mochi", "bubble", "mech", "claw"}


class TestMochiFace:
    def _face(self):
        from render.faces.mochi import MochiFace
        return MochiFace()

    def test_render_normal(self) -> None:
        face = self._face()
        face.set_expression("normal")
        face.tick(0.033)
        frame = face.render()
        assert frame.width == 256 and frame.height == 32
        assert len(frame.pixels) == 256 * 32 * 3
        # has non-black pixels (eyes)
        assert any(frame.pixels[i] > 0 for i in range(0, len(frame.pixels), 3))

    def test_expression_changes_pixels(self) -> None:
        face = self._face()
        face.set_expression("normal")
        face.tick(0.033)
        f1 = face.render()

        face.set_expression("surprised")
        face.tick(0.033)
        f2 = face.render()

        assert f1.pixels != f2.pixels  # different expression = different frame

    def test_sleepy_flat_eyes(self) -> None:
        face = self._face()
        face.set_expression("sleepy")
        face.tick(0.033)
        sleepy_frame = face.render()
        lit_sleepy = sum(1 for i in range(0, len(sleepy_frame.pixels), 3)
                         if sleepy_frame.pixels[i] > 10)

        face.set_expression("normal")
        face.tick(0.033)
        normal_frame = face.render()
        lit_normal = sum(1 for i in range(0, len(normal_frame.pixels), 3)
                         if normal_frame.pixels[i] > 10)

        assert lit_sleepy < lit_normal  # sleepy eyes are tiny < normal eyes

    def test_update_with_emotion(self) -> None:
        face = self._face()
        face.update(emotion="happy")
        assert face.state.expression == "happy"

    def test_update_with_amplitude(self) -> None:
        face = self._face()
        face.update(tts_amplitude=0.5)
        assert face.state.mouth_open == 0.5


class TestBubbleFace:
    def _face(self):
        from render.faces.bubble import BubbleFace
        return BubbleFace()

    def test_render(self) -> None:
        face = self._face()
        face.tick(0.033)
        frame = face.render()
        assert frame.width == 256 and frame.height == 32

    def test_highlight_visible(self) -> None:
        """Bubble face should have near-white highlight pixels."""
        face = self._face()
        face.set_expression("normal")
        face.tick(0.033)
        frame = face.render()
        # check for bright pixels (highlight = 220,245,255)
        has_highlight = any(
            frame.pixels[i] > 200 and frame.pixels[i + 1] > 220
            for i in range(0, len(frame.pixels) - 2, 3)
        )
        assert has_highlight, "no highlight pixels found"

    def test_expression_change(self) -> None:
        face = self._face()
        face.set_expression("normal")
        face.tick(0.033)
        f1 = face.render()
        face.set_expression("angry")
        face.tick(0.033)
        f2 = face.render()
        assert f1.pixels != f2.pixels


class TestMechFace:
    def _face(self):
        from render.faces.mech import MechFace
        return MechFace()

    def test_render(self) -> None:
        face = self._face()
        face.tick(0.033)
        frame = face.render()
        assert frame.width == 256 and frame.height == 32

    def test_expression_change(self) -> None:
        face = self._face()
        face.set_expression("normal")
        face.tick(0.033)
        f1 = face.render()
        face.set_expression("surprised")
        face.tick(0.033)
        f2 = face.render()
        assert f1.pixels != f2.pixels

    def test_scanning_line(self) -> None:
        """Mech face has a scanning animation line."""
        face = self._face()
        face.tick(0.0)
        face.tick(0.5)  # advance scan
        frame = face.render()
        # scanning line should add some dim blue pixels
        # (not a strict test, but checks the animation runs)
        assert len(frame.pixels) == 256 * 32 * 3


class TestFaceSwitchIntegration:
    """FaceManager + bus event integration."""

    async def test_face_switch_via_bus(self) -> None:
        from core.bus import topics
        from core.bus.hub import InProcBus
        from render.faces.manager import FaceManager

        bus = InProcBus()
        fm = FaceManager(style="claw")

        async def on_switch(topic, payload):
            fm.switch(payload.get("style", "claw"))

        bus.subscribe(topics.FACE_SWITCH, on_switch)
        await bus.publish(topics.FACE_SWITCH, {"style": "mochi"})
        assert fm.style == "mochi"

        await bus.publish(topics.FACE_SWITCH, {"style": "mech"})
        assert fm.style == "mech"

    async def test_all_faces_render_valid_frames(self) -> None:
        from render.faces.manager import FaceManager

        fm = FaceManager()
        for style in ("wafer", "mochi", "bubble", "mech", "claw"):
            fm.switch(style)
            fm.set_expression("happy")
            fm.tick(0.033)
            frame = fm.render()
            assert frame.width == 256
            assert frame.height == 32
            assert len(frame.pixels) == 256 * 32 * 3
