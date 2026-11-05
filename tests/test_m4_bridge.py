"""Tests for M4: bridge (composer + session book + dispatch) + ctl + guards."""

from bridge.composer import TaskComposer
from bridge.server import Bridge, SessionBook
from core.bus import topics
from core.bus.hub import InProcBus
from core.interfaces import Route
from core.router.scorer import RouterRules, SpeakerContext, TaskContext, route
from ctl import CtlService


class TestTaskComposer:
    def test_compose_basic(self) -> None:
        composer = TaskComposer()
        task = composer.compose("帮我修一下登录报错", "master")
        assert task.speaker_id == "master"
        assert "修" in task.prompt or "登录" in task.prompt
        assert task.needs_confirmation is True
        assert task.composed is True
        assert task.task_id.startswith("t-")

    def test_compose_with_context(self) -> None:
        composer = TaskComposer()
        task = composer.compose(
            "把刚才那个再改一下", "master",
            recent_context=["我们发现了登录页面的bug", "是路径问题"],
        )
        assert "refers to" in task.prompt or "刚才" in task.prompt

    def test_compose_cleans_fillers(self) -> None:
        composer = TaskComposer()
        task = composer.compose("嗯那个帮我跑一下测试啊", "master")
        assert "嗯" not in task.prompt
        assert "那个" not in task.prompt

    def test_amend(self) -> None:
        composer = TaskComposer()
        original = composer.compose("帮我修登录报错", "master")
        amended = composer.amend(original, "不对，是改按钮颜色")
        assert "改按钮颜色" in amended.prompt
        assert amended.task_id == original.task_id

    def test_unique_task_ids(self) -> None:
        composer = TaskComposer()
        t1 = composer.compose("task1", "master")
        t2 = composer.compose("task2", "master")
        assert t1.task_id != t2.task_id


class TestSessionBook:
    def test_persist_and_reload(self, tmp_path) -> None:
        book_path = tmp_path / "session_book.json"
        book1 = SessionBook(book_path)
        book1.set("project_a", "session_123")
        book1.set("project_b", "session_456")

        book2 = SessionBook(book_path)
        assert book2.get("project_a") == "session_123"
        assert book2.get("project_b") == "session_456"

    def test_reset(self, tmp_path) -> None:
        book = SessionBook(tmp_path / "sb.json")
        book.set("proj", "sid")
        book.reset("proj")
        assert book.get("proj") is None

    def test_corrupted_file_starts_fresh(self, tmp_path) -> None:
        path = tmp_path / "bad.json"
        path.write_text("not valid json{")
        book = SessionBook(path)
        assert book.get("anything") is None


class TestBridge:
    async def test_dispatch_produces_events(self) -> None:
        bus = InProcBus()
        events: list[tuple[str, dict]] = []
        bus.subscribe("*", lambda t, p: events.append((t, dict(p))))

        bridge = Bridge(bus=bus, default_project=".")
        task = await bridge.dispatch("帮我修一下登录", "master")

        assert task.speaker_id == "master"
        topics_seen = [t for t, _ in events]
        assert topics.TASK_PROGRESS in topics_seen
        assert topics.TASK_RESULT in topics_seen

    async def test_cancel_with_active_task(self) -> None:
        """Cancel works when there IS an active task (simulated)."""
        bus = InProcBus()
        cancel_events: list[dict] = []
        bus.subscribe(topics.TASK_CANCEL, lambda t, p: cancel_events.append(p))

        bridge = Bridge(bus=bus)
        # Manually set an active task (dispatch completes too fast in sim)
        bridge._active_task = TaskComposer().compose("test task", "master")
        await bridge.cancel("test")

        assert len(cancel_events) == 1
        assert cancel_events[0]["reason"] == "test"

    async def test_reset_session(self, tmp_path) -> None:
        bus = InProcBus()
        bridge = Bridge(bus=bus, session_book_path=tmp_path / "sb.json")
        bridge.session_book.set("proj", "sid")
        await bridge.reset_session("proj")
        assert bridge.session_book.get("proj") is None


class TestCtlService:
    async def test_hotkey_publishes_event(self) -> None:
        bus = InProcBus()
        events: list[dict] = []
        bus.subscribe(topics.CTL_HOTKEY, lambda t, p: events.append(p))

        ctl = CtlService(bus=bus)
        await ctl.fire_hotkey("wake")
        assert len(events) == 1
        assert events[0]["action"] == "wake"

    async def test_notify_publishes_event(self) -> None:
        bus = InProcBus()
        events: list[dict] = []
        bus.subscribe(topics.SYSTEM_NOTIFY, lambda t, p: events.append(p))

        ctl = CtlService(bus=bus)
        await ctl.notify("warn", "test notification")
        assert len(events) == 1
        assert events[0]["text"] == "test notification"

    def test_default_hotkeys_defined(self) -> None:
        bus = InProcBus()
        ctl = CtlService(bus=bus)
        assert "wake" in ctl.hotkeys
        assert "gate_toggle" in ctl.hotkeys
        assert "approve" in ctl.hotkeys


class TestGuardrails:
    """T4.9: CI guard tests for architectural invariants."""

    def test_task_composer_only_reorganizes(self) -> None:
        """D14: Composer must not add technical content not in the transcript."""
        composer = TaskComposer()
        task = composer.compose("帮我看看天气", "master")
        code_keywords = ["pytest", "npm", "git", "docker", "deploy"]
        for kw in code_keywords:
            assert kw not in task.prompt.lower(), (
                f"Composer added '{kw}' which wasn't in original"
            )

    def test_bridge_is_sole_opencode_channel(self) -> None:
        """D4: Only bridge module should reference opencode subprocess."""
        import core.agent
        import core.pipeline
        import core.router.scorer
        assert not hasattr(core.pipeline, "subprocess")
        assert not hasattr(core.agent, "subprocess")

    def test_route_admin_only_for_tasks(self) -> None:
        """D12: member cannot dispatch tasks."""
        rules = RouterRules()
        member = SpeakerContext(role="member")
        admin = SpeakerContext(role="admin")
        no_task = TaskContext(active=False)

        assert route("帮我修一下报错", admin, rules, no_task) == Route.TASK_DIRECT
        assert route("帮我修一下报错", member, rules, no_task) == Route.TASK_DENIED

    def test_amend_only_with_active_task(self) -> None:
        """D15: Amend/cancel only when task is active."""
        rules = RouterRules()
        admin = SpeakerContext(role="admin")
        no_task = TaskContext(active=False)
        active = TaskContext(active=True, task_id="t-1")

        assert route("不对，是这样的", admin, rules, no_task) != Route.TASK_AMEND
        assert route("不对，是这样的", admin, rules, active) == Route.TASK_AMEND
