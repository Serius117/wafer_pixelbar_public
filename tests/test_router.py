"""Tests for intent router (design doc §4.3, D14/D15/D16)."""

from core.interfaces import Route
from core.router.scorer import RouterRules, SpeakerContext, TaskContext, route, score_text

RULES = RouterRules()
ADMIN = SpeakerContext(role="admin")
MEMBER = SpeakerContext(role="member")
NO_TASK = TaskContext(active=False)
ACTIVE_TASK = TaskContext(active=True, task_id="t-1")


class TestScoreText:
    def test_plain_chat_scores_zero(self) -> None:
        assert score_text("今天好累啊", RULES) == 0.0

    def test_verb_only(self) -> None:
        assert score_text("修一下", RULES) == 1.0

    def test_noun_only(self) -> None:
        assert score_text("那个报错", RULES) == 1.0

    def test_context_only(self) -> None:
        assert score_text("帮我", RULES) == 0.5

    def test_verb_noun_context_combo(self) -> None:
        """修(1) + 报错(1) + 帮我(0.5) + combo(1) = 3.5."""
        assert score_text("帮我修一下登录报错", RULES) == 3.5

    def test_git_book_scores_low(self) -> None:
        """'讲 git 的书' hits git(noun) only -> 1.0 (gray zone)."""
        assert score_text("我昨天看了本讲 git 的书", RULES) == 1.0


class TestRouteChat:
    def test_plain_chat(self) -> None:
        assert route("今天好累啊", ADMIN, RULES, NO_TASK) == Route.CHAT

    def test_empty_text(self) -> None:
        assert route("", ADMIN, RULES, NO_TASK) == Route.CHAT

    def test_git_book_goes_to_llm(self) -> None:
        """Score 1.0 is in gray zone [1.0, 2.5) -> LLM decides."""
        assert route("我昨天看了本讲 git 的书", ADMIN, RULES, NO_TASK) == Route.LLM


class TestRouteTaskDirect:
    def test_confident_coding_intent_admin(self) -> None:
        assert route("帮我修一下登录报错", ADMIN, RULES, NO_TASK) == Route.TASK_DIRECT

    def test_document_generation_is_direct_task(self) -> None:
        """Document/report nouns must not fall into the gray chat band."""
        text = "帮我创建一个新的 Markdown 文档，总结一下目前的进度"
        assert score_text(text, RULES) >= RULES.s_direct
        assert route(text, ADMIN, RULES, NO_TASK) == Route.TASK_DIRECT

    def test_report_generation_is_direct_task(self) -> None:
        assert route("帮我写一份项目报告", ADMIN, RULES,
                     NO_TASK) == Route.TASK_DIRECT

    def test_member_denied(self) -> None:
        assert route("帮我修一下登录报错", MEMBER, RULES, NO_TASK) == Route.TASK_DENIED

    def test_fast_lane_prefix_admin(self) -> None:
        """D14: trigger prefix bypasses scoring entirely."""
        assert route("派活，把刚才那个问题弄一下", ADMIN, RULES, NO_TASK) == Route.TASK_DIRECT

    def test_fast_lane_prefix_member(self) -> None:
        assert route("派活，把刚才那个问题弄一下", MEMBER, RULES, NO_TASK) == Route.TASK_DENIED

    def test_fast_lane_mid_sentence_not_triggered(self) -> None:
        """Prefix must be at the start of the utterance."""
        assert route("你说派活是什么意思", ADMIN, RULES, NO_TASK) != Route.TASK_DIRECT


class TestRouteSessionEnd:
    def test_baibai(self) -> None:
        assert route("拜拜", ADMIN, RULES, NO_TASK) == Route.SESSION_END

    def test_haole(self) -> None:
        assert route("好了", ADMIN, RULES, NO_TASK) == Route.SESSION_END

    def test_wanan(self) -> None:
        assert route("晚安", ADMIN, RULES, NO_TASK) == Route.SESSION_END

    def test_baibai_with_other_words(self) -> None:
        assert route("今天聊得很开心，拜拜", ADMIN, RULES, NO_TASK) == Route.SESSION_END


class TestRouteAmend:
    def test_amend_with_active_task(self) -> None:
        assert route("不对，是这样的：改按钮颜色", ADMIN, RULES, ACTIVE_TASK) == Route.TASK_AMEND

    def test_amend_without_task_goes_to_chat(self) -> None:
        """Amend cues only fire when a task is active (D15 anti-mistouch)."""
        assert route("不对，是这样的：改按钮颜色", ADMIN, RULES, NO_TASK) != Route.TASK_AMEND

    def test_cancel_with_active_task(self) -> None:
        assert route("算了别弄了", ADMIN, RULES, ACTIVE_TASK) == Route.TASK_CANCEL

    def test_said_wrong(self) -> None:
        assert route("说错了，是改颜色", ADMIN, RULES, ACTIVE_TASK) == Route.TASK_AMEND


class TestRouteReset:
    def test_reset_cue(self) -> None:
        assert route("新开会话", ADMIN, RULES, NO_TASK) == Route.TASK_RESET

    def test_reset_mid_conversation(self) -> None:
        assert route("重头搞，换个思路", ADMIN, RULES, ACTIVE_TASK) == Route.TASK_RESET


class TestRoutePriority:
    """Rule priority: reset > amend/cancel > exit > fast-lane > score."""

    def test_reset_beats_amend(self) -> None:
        assert route("新开会话，不对不对", ADMIN, RULES, ACTIVE_TASK) == Route.TASK_RESET

    def test_exit_beats_score(self) -> None:
        """Exit words checked before scoring, so this ends the session
        even though it mentions coding nouns."""
        result = route("帮我修完这个bug然后拜拜", ADMIN, RULES, NO_TASK)
        assert result == Route.SESSION_END

    def test_fast_lane_beats_score(self) -> None:
        """Fast lane prefix would give TASK_DIRECT even with no coding nouns."""
        assert route("开工", ADMIN, RULES, NO_TASK) == Route.TASK_DIRECT


class TestEdgeCases:
    def test_whitespace_only(self) -> None:
        assert route("   ", ADMIN, RULES, NO_TASK) == Route.CHAT

    def test_english_text(self) -> None:
        """English text has no Chinese keyword hits -> CHAT."""
        assert route("hello world", ADMIN, RULES, NO_TASK) == Route.CHAT

    def test_mixed_language(self) -> None:
        """'帮我 fix 一下 bug' hits 帮我(ctx) + bug(noun) = 1.5 -> gray zone."""
        assert route("帮我 fix 一下 bug", ADMIN, RULES, NO_TASK) == Route.LLM
