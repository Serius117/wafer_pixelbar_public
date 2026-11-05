"""ROUTE-15..26 boundary / misfire / ordering tests (TEST_DESIGN §4.2).

Note on ROUTE-16: the designed "S exactly 2.5" is NOT constructible with
the default rules (verb+noun always adds the +1 combo -> 3.0; verb+ctx or
noun+ctx is 1.5). The real boundaries are 3.0 (task band) and 1.5/1.0
(fuzzy band) -- tested here and annotated back into TEST_DESIGN.
"""

from core.interfaces import Route
from core.router.scorer import (
    RouterRules,
    SpeakerContext,
    TaskContext,
    route,
    score_text,
)

ADMIN = SpeakerContext(role="admin")
MEMBER = SpeakerContext(role="member")
ACTIVE = TaskContext(active=True, task_id="t-1")
INACTIVE = TaskContext(active=False)
RULES = RouterRules()


class TestRoutePrefixAnchoring:
    def test_route_15_prefix_must_be_initial(self) -> None:
        """"派活" mid-sentence does not use the fast lane (D14 anchoring)."""
        assert route("我说派活两个字", ADMIN, RULES, INACTIVE) == Route.CHAT

    def test_route_25_amend_beats_fast_lane(self) -> None:
        """Order: reset -> amend/cancel -> exit -> prefix -> score."""
        result = route("派活，不对改成蓝色", ADMIN, RULES, ACTIVE)
        assert result == Route.TASK_AMEND


class TestRouteScoreBoundaries:
    def test_route_16_task_band_lower_edge(self) -> None:
        """verb+noun without context words = 1+1+1(combo) = 3.0 >= 2.5 -> task."""
        text = "修一下报错"  # no 帮我
        assert score_text(text, RULES) == 3.0
        assert route(text, ADMIN, RULES, INACTIVE) == Route.TASK_DIRECT

    def test_route_16_fuzzy_upper_edge(self) -> None:
        """verb+ctx only = 1.5 < 2.5 -> fuzzy band (LLM decides)."""
        text = "帮我修一下"  # no coding noun
        assert score_text(text, RULES) == 1.5
        assert route(text, ADMIN, RULES, INACTIVE) == Route.LLM

    def test_route_17_gray_boundary_inclusive(self) -> None:
        """noun only = 1.0 == s_gray -> LLM (>= semantics)."""
        text = "报错"
        assert score_text(text, RULES) == 1.0
        assert route(text, ADMIN, RULES, INACTIVE) == Route.LLM

    def test_route_17_below_gray_is_chat(self) -> None:
        assert route("今天天气真好", ADMIN, RULES, INACTIVE) == Route.CHAT

    def test_route_18_multi_verb_counts_once(self) -> None:
        assert score_text("写个再改改", RULES) == 1.0  # two verbs, one point

    def test_route_19_full_combination(self) -> None:
        """ctx + verb + noun + combo = 0.5+1+1+1 = 3.5, combo only once."""
        assert score_text("帮我修一下登录报错", RULES) == 3.5


class TestRouteMisfireLocks:
    """Substring misfires: locked as current behaviour (TEST_DESIGN P1 notes)."""

    def test_route_20_exit_word_substring(self) -> None:
        assert route("明天见，再见", ADMIN, RULES, INACTIVE) == Route.SESSION_END

    def test_route_21_cancel_word_substring(self) -> None:
        # "停" hits the cancel cues while the user meant "stop and chat"
        assert route("停下来陪我聊聊", ADMIN, RULES, ACTIVE) == Route.TASK_CANCEL

    def test_route_22_amend_word_substring(self) -> None:
        assert route("你说的不对", ADMIN, RULES, ACTIVE) == Route.TASK_AMEND

    def test_route_23_reset_without_active_task(self) -> None:
        """Reset cues fire even with no active task (locked current behaviour)."""
        assert route("重新开始吧", ADMIN, RULES, INACTIVE) == Route.TASK_RESET

    def test_route_12_amend_requires_active(self) -> None:
        assert route("不对，你说错了", ADMIN, RULES, INACTIVE) != Route.TASK_AMEND


class TestRouteMisc:
    def test_route_24_english_mixed_input(self) -> None:
        """"bug" is in the noun table -> S=1.0 -> fuzzy band."""
        assert route("fix the bug please", ADMIN, RULES, INACTIVE) == Route.LLM

    def test_route_26_default_speaker_role_is_admin(self) -> None:
        """Locked current behaviour: default role grants admin -- deployment
        MUST always pass an explicit role (guard on the wiring side)."""
        assert SpeakerContext().role == "admin"
