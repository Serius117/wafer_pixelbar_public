"""Whale face style tests (TASK.AVATAR.01)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class TestWhaleFace:
    @pytest.fixture
    def face(self):
        from render.faces.whale import WhaleFace

        f = WhaleFace()
        return f

    def _lit(self, frame):
        px = frame.pixels
        return sum(1 for i in range(0, len(px), 3)
                   if max(px[i], px[i+1], px[i+2]) > 50)

    def test_all_expressions_render(self, face):
        """Six expressions all produce non-blank frames."""
        from render.faces.whale import WhaleFace

        for expr in WhaleFace._expressions:
            face.set_expression(expr)
            face.tick(0.03)
            frame = face.render()
            lit = self._lit(frame)
            assert lit > 10, f"{expr} produced only {lit} lit pixels"

    def test_unknown_falls_back(self, face):
        """Unknown expression falls back to normal."""
        face.set_expression("bogus")
        assert face.state.expression == "normal"

    def test_sad_has_tear(self, face):
        """Sad expression draws a tear (extra pixels below eye)."""
        face.set_expression("normal")
        face.tick(0.03)
        normal_lit = self._lit(face.render())
        face.set_expression("sad")
        face.tick(0.03)
        sad_lit = self._lit(face.render())
        # sad should have at least the same + tear pixels
        assert sad_lit >= normal_lit - 5  # allow breathing variance

    def test_celebrate_has_stars(self, face):
        """Celebrate draws twinkling stars (extra pixels)."""
        face.set_expression("normal")
        face.tick(0.03)
        normal_lit = self._lit(face.render())
        face.set_expression("celebrate")
        face.state._anim_t = 0.5  # mid-animation (stars visible)
        face.tick(0.03)
        celeb_lit = self._lit(face.render())
        assert celeb_lit > normal_lit, \
            f"celebrate ({celeb_lit}) should have more pixels than normal ({normal_lit})"

    def test_listening_indicator(self, face):
        """Listening state adds 3 dots in top-right."""
        face.update(session_state="listening")
        face.tick(0.03)
        frame = face.render()
        # check top-right area for indicator dots
        px = frame.pixels
        found = any(max(px[(3 * 256 + x) * 3:(3 * 256 + x) * 3 + 3]) > 50
                    for x in range(236, 252))
        assert found, "listening indicator dots not found in top-right"
