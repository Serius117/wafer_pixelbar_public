"""Intent router: deterministic scoring + specialized rules (design doc §4.3).

Pure function module -- zero I/O. The router decides where a transcript goes:
  CHAT          -> pure small talk (local 9B handles it)
  LLM           -> ambiguous band, local 9B decides (it has the tool)
  TASK_DIRECT   -> confident coding intent, straight to bridge/opencode
  TASK_DENIED   -> member coding request, deterministic refusal
  SESSION_END   -> "拜拜" closes the session
  TASK_AMEND    -> correction cues while a task is active (D15)
  TASK_CANCEL   -> cancel cues while a task is active (D15)
  TASK_RESET    -> reset the working session (D16/D18)

Scoring (all rules from config/router.yaml, deterministic):
  action verb  +1.0   (写/改/修/跑/执行/部署/编译/重构/提交/安装)
  coding noun  +1.0   (代码/文件/函数/接口/报错/bug/测试/脚本/项目/git)
  context      +0.5   (帮我/让它/交给/任务)
  combo bonus  +1.0   (verb + noun both hit)

Thresholds: S_direct=2.5, S_gray=1.0.

Fast lane (D14): utterance starting with a trigger prefix bypasses scoring.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.interfaces import Route


@dataclass(frozen=True, slots=True)
class RouterRules:
    """All routing rules and thresholds, loaded from router.yaml."""

    # Scoring keywords (D35: added 查 -- the flagship dispatch sentence
    # "帮我查一下…测试文件" lost its fast-lane prefix 派活 through real ASR
    # ~50% of runs (VE2E V.2 finding) and scored 1.5 = gray band -> chat.
    # With 查 as a verb it scores 3.5 -> TASK_DIRECT regardless of prefix.)
    action_verbs: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {"写", "改", "修", "跑", "执行", "部署", "编译", "重构", "提交",
             "安装", "删", "创建", "查", "看一下", "数一下"}
        )
    )
    coding_nouns: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {"代码", "文件", "函数", "接口", "报错", "bug", "测试", "脚本", "项目", "git", "终端",
             "文档", "Markdown", "markdown", "报告"}
        )
    )
    context_words: frozenset[str] = field(
        default_factory=lambda: frozenset({"帮我", "让它", "交给", "任务"})
    )

    # Thresholds
    s_direct: float = 2.5
    s_gray: float = 1.0

    # Fast lane prefixes (D14): sentence-initial trigger words
    trigger_prefixes: tuple[str, ...] = ("派活", "开工")

    # Exit words (session end)
    exit_words: frozenset[str] = field(
        default_factory=lambda: frozenset({"拜拜", "好了", "再见", "晚安"})
    )

    # Amend/cancel cues (D15)
    amend_cues: frozenset[str] = field(
        default_factory=lambda: frozenset({"不对", "不是", "说错了", "等等", "弄错了"})
    )
    cancel_cues: frozenset[str] = field(
        default_factory=lambda: frozenset({"取消", "算了", "停", "别弄了", "别做了"})
    )

    # Reset cues (D16/D18)
    reset_cues: frozenset[str] = field(
        default_factory=lambda: frozenset({"新开会话", "重头搞", "重新开始"})
    )


@dataclass(frozen=True, slots=True)
class TaskContext:
    """Whether a task is currently active (for amend/cancel/reset rules)."""

    active: bool = False
    task_id: str | None = None


@dataclass(frozen=True, slots=True)
class SpeakerContext:
    """Speaker role for permission checking."""

    role: str = "admin"  # "admin" | "member"


def score_text(text: str, rules: RouterRules) -> float:
    """Calculate the coding-intent score for a transcript."""
    score = 0.0
    has_verb = False
    has_noun = False

    for verb in rules.action_verbs:
        if verb in text:
            score += 1.0
            has_verb = True
            break  # only count once

    for noun in rules.coding_nouns:
        if noun in text:
            score += 1.0
            has_noun = True
            break  # only count once

    for ctx in rules.context_words:
        if ctx in text:
            score += 0.5
            break  # only count once

    if has_verb and has_noun:
        score += 1.0  # combo bonus

    return score


def _starts_with_any(text: str, prefixes: tuple[str, ...]) -> bool:
    return any(text.startswith(p) for p in prefixes)


def _contains_any(text: str, words: frozenset[str]) -> bool:
    return any(w in text for w in words)


def route(
    text: str,
    speaker: SpeakerContext,
    rules: RouterRules,
    task_ctx: TaskContext | None = None,
) -> Route:
    """Route a transcript to its destination.

    Args:
        text: The ASR transcript (already gate-passed).
        speaker: Speaker context (role for permission check).
        rules: Routing rules from config.
        task_ctx: Whether a task is active (for amend/cancel/reset).

    Returns:
        Route enum value.
    """
    text = text.strip()
    if not text:
        return Route.CHAT

    # Session reset (D16/D18): checked before everything else
    if task_ctx is not None and _contains_any(text, rules.reset_cues):
        return Route.TASK_RESET

    # Task amend/cancel (D15): only when a task is active
    if task_ctx is not None and task_ctx.active:
        if _contains_any(text, rules.amend_cues):
            return Route.TASK_AMEND
        if _contains_any(text, rules.cancel_cues):
            return Route.TASK_CANCEL

    # Exit words (session end)
    if _contains_any(text, rules.exit_words):
        return Route.SESSION_END

    # Fast lane (D14): trigger prefix bypasses scoring
    if _starts_with_any(text, rules.trigger_prefixes):
        if speaker.role == "admin":
            return Route.TASK_DIRECT
        return Route.TASK_DENIED

    # Score-based routing
    score = score_text(text, rules)

    if score >= rules.s_direct:
        if speaker.role == "admin":
            return Route.TASK_DIRECT
        return Route.TASK_DENIED

    if score >= rules.s_gray:
        return Route.LLM  # ambiguous: local 9B decides

    return Route.CHAT
