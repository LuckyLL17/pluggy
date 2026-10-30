"""
Tests for immutable :class:`PluginManagerSnapshot` capture and isolated
clone restoration.
"""

from typing import Any
import warnings

import pytest

from pluggy import _snapshot as snapshot_module
from pluggy import HookimplMarker
from pluggy import HookspecMarker
from pluggy import PluginManager
from pluggy import PluginManagerSnapshot
from pluggy import SnapshotCrossManagerError
from pluggy import SnapshotDestroyedError
from pluggy import SnapshotError
from pluggy import SnapshotExpiredError
from pluggy import SnapshotReusedError


hookspec = HookspecMarker("example")
hookimpl = HookimplMarker("example")


class Spec:
    @hookspec
    def he_method1(self, arg: int) -> object: ...

    @hookspec(historic=True)
    def configure(self, arg: int) -> None: ...


class FirstResultSpec:
    @hookspec(firstresult=True)
    def first(self, arg: int) -> object: ...


class Plugin1:
    def __init__(self, log: list[Any] | None = None) -> None:
        self.log = log

    @hookimpl(tryfirst=True)
    def he_method1(self, arg: int) -> int:
        if self.log is not None:
            self.log.append(("p1", arg))
        return int(arg) + 1

    @hookimpl
    def configure(self, arg: int) -> None:
        if self.log is not None:
            self.log.append(("p1-conf", arg))


class Plugin2:
    def __init__(self, log: list[Any] | None = None) -> None:
        self.log = log

    @hookimpl
    def he_method1(self, arg: int) -> int:
        if self.log is not None:
            self.log.append(("p2", arg))
        return int(arg) + 10

    @hookimpl
    def configure(self, arg: int) -> None:
        if self.log is not None:
            self.log.append(("p2-conf", arg))


def _populated_manager(
    log: list[Any],
) -> tuple[PluginManager, Plugin1, Plugin2]:
    pm = PluginManager("example")
    pm.add_hookspecs(Spec)
    p1 = Plugin1(log)
    p2 = Plugin2(log)
    pm.register(p1, "p1")
    pm.register(p2, "p2")
    pm.set_blocked("banned")
    pm.hook.configure.call_historic(kwargs={"arg": 1})
    log.clear()
    return pm, p1, p2


# --------------------------------------------------------------------------- #
# Capture                                                                      #
# --------------------------------------------------------------------------- #


def test_snapshot_of_empty_manager(pm: PluginManager) -> None:
    snap = pm.snapshot()
    assert snap.project_name == "example"
    assert snap.manager_version == 0
    assert snap.plugin_names == ()
    assert snap.blocked_names == frozenset()
    assert snap.plugins == ()
    assert snap.hook_names == ()
    assert not snap.is_consumed
    assert not snap.is_destroyed
    assert not snap.is_expired


def test_snapshot_captures_full_state() -> None:
    log: list[Any] = []
    pm, p1, p2 = _populated_manager(log)

    snap = pm.snapshot()
    assert snap.plugin_names == ("p1", "p2")
    assert snap.plugins == (p1, p2)
    assert snap.blocked_names == frozenset({"banned"})
    assert tuple(sorted(snap.hook_names)) == ("configure", "he_method1")
    assert snap.manager_version == pm.manager_version

    # Hook implementation order and metadata are recorded exactly.
    clone = snap.restore()
    src_impls = pm.hook.he_method1.get_hookimpls()
    clone_impls = clone.hook.he_method1.get_hookimpls()
    assert [i.plugin for i in clone_impls] == [i.plugin for i in src_impls]
    assert [i.tryfirst for i in clone_impls] == [i.tryfirst for i in src_impls]
    # HookImpl and HookSpec are immutable and may be shared.
    assert clone_impls[0] is src_impls[0]
    assert clone.hook.he_method1.spec is pm.hook.he_method1.spec


def test_snapshot_is_point_in_time() -> None:
    log: list[Any] = []
    pm, _p1, _p2 = _populated_manager(log)
    snap = pm.snapshot()

    class Plugin3:
        @hookimpl
        def he_method1(self, arg: int) -> int:
            return int(arg) + 100

    pm.register(Plugin3(), "p3")
    pm.set_blocked("p1")

    class ExtraSpec:
        @hookspec
        def extra(self, arg: int) -> None: ...

    pm.add_hookspecs(ExtraSpec)

    # The existing snapshot keeps its captured state.
    assert snap.plugin_names == ("p1", "p2")
    assert snap.blocked_names == frozenset({"banned"})
    assert "extra" not in snap.hook_names

    # And restores to that state, even though the source moved on.
    clone = snap.restore()
    assert clone.has_plugin("p1")
    assert not clone.has_plugin("p3")
    assert not clone.is_blocked("p1")
    assert not hasattr(clone.hook, "extra")


def test_snapshot_freezes_historic_kwargs() -> None:
    pm = PluginManager("example")
    pm.add_hookspecs(Spec)
    kwargs = {"arg": 1}
    pm.hook.configure.call_historic(kwargs=kwargs)
    snap = pm.snapshot()
    # Mutating the caller's dict after the fact must not change the record.
    kwargs["arg"] = 999

    received: list[Any] = []

    class Late:
        @hookimpl
        def configure(self, arg: int) -> None:
            received.append(arg)

    clone = snap.restore()
    clone.register(Late())
    assert received == [1]


def test_manager_version_tracks_structural_changes(pm: PluginManager) -> None:
    assert pm.manager_version == 0
    pm.add_hookspecs(Spec)
    assert pm.manager_version == 1

    class P:
        @hookimpl
        def he_method1(self, arg: int) -> None: ...

    p = P()
    pm.register(p, "p")
    assert pm.manager_version == 2
    # No-ops do not bump the version.
    pm.register(None)
    pm.set_blocked("p")  # unregisters p and blocks the name: one change.
    assert pm.manager_version == 3
    pm.set_blocked("p")  # already blocked
    assert pm.manager_version == 3
    pm.unregister(p, "p")  # blocked name, no state change
    assert pm.manager_version == 3
    assert pm.unblock("p") is True
    assert pm.manager_version == 4
    assert pm.unblock("p") is False
    assert pm.manager_version == 4


# --------------------------------------------------------------------------- #
# Clone construction                                                           #
# --------------------------------------------------------------------------- #


def test_restore_builds_independent_manager() -> None:
    log: list[Any] = []
    pm, _p1, _p2 = _populated_manager(log)
    snap = pm.snapshot()
    clone = snap.restore()

    assert type(clone) is PluginManager
    assert clone is not pm
    assert clone.project_name == "example"
    assert clone.list_name_plugin() == pm.list_name_plugin()
    assert clone.is_blocked("banned")
    # Underlying containers are distinct objects.
    assert clone._name2plugin is not pm._name2plugin
    assert clone.hook is not pm.hook
    assert clone.manager_version == snap.manager_version == pm.manager_version


def test_restore_runs_no_plugin_callbacks() -> None:
    log: list[Any] = []
    pm, _p1, _p2 = _populated_manager(log)
    log.clear()

    snap = pm.snapshot()
    assert log == []
    clone = snap.restore()
    assert log == []
    # The forked plugins are wired up but their historic events were not
    # replayed while building the clone.
    clone.unregister(clone.get_plugin("p1"))
    clone.register(clone.get_plugin("p1"), "p1")
    assert log == []


def test_restore_via_manager_method_runs_no_callbacks() -> None:
    log: list[Any] = []
    pm, _p1, _p2 = _populated_manager(log)
    log.clear()
    target = PluginManager("example")
    target.restore_snapshot(pm.snapshot())
    assert log == []
    assert target.has_plugin("p1")


def test_blocked_name_stays_blocked_in_clone() -> None:
    log: list[Any] = []
    pm, _p1, _p2 = _populated_manager(log)
    clone = pm.snapshot().restore()
    assert clone.is_blocked("banned")
    # The block is functional: a later registration under that name is
    # rejected and never lands in the clone.
    rejected = object()
    assert clone.register(rejected, "banned") is None
    assert not clone.is_registered(rejected)
    assert not pm.is_registered(rejected)


def test_restore_does_not_trigger_warn_on_impl() -> None:
    class WarnSpec:
        @hookspec(warn_on_impl=DeprecationWarning("old hook"))
        def old_hook(self) -> None: ...

    class Warned:
        @hookimpl
        def old_hook(self) -> int:
            return 1

    class AlsoWarned:
        @hookimpl
        def old_hook(self) -> int:
            return 2

    pm = PluginManager("example")
    pm.add_hookspecs(WarnSpec)
    with pytest.warns(DeprecationWarning):
        pm.register(Warned())
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        clone = pm.snapshot().restore()
    assert not any(issubclass(w.category, DeprecationWarning) for w in recorded)
    # The spec is fully active in the clone: new implementations still warn.
    with pytest.warns(DeprecationWarning):
        clone.register(AlsoWarned())


def test_restore_does_not_modify_source() -> None:
    log: list[Any] = []
    pm, p1, p2 = _populated_manager(log)
    before_names = pm.list_name_plugin()
    before_impls = [i.plugin for i in pm.hook.he_method1.get_hookimpls()]
    before_version = pm.manager_version
    source_history = pm.hook.configure._call_history
    assert source_history is not None
    before_history_len = len(source_history)

    clone = pm.snapshot().restore()

    class Plugin3:
        @hookimpl
        def he_method1(self, arg: int) -> int:
            return int(arg) + 100

    clone.register(Plugin3(), "p3")
    clone.unregister(clone.get_plugin("p1"))
    clone.set_blocked("p2")
    clone.hook.configure.call_historic(kwargs={"arg": 7})

    assert pm.list_name_plugin() == before_names
    assert pm.has_plugin("p1") and pm.has_plugin("p2")
    assert [i.plugin for i in pm.hook.he_method1.get_hookimpls()] == before_impls
    assert pm.manager_version == before_version
    assert pm.hook.configure._call_history is source_history
    assert len(source_history) == before_history_len
    assert not pm.is_blocked("p2")
    # Fork plugins are shared identities (immutable metadata), while the
    # clone's removals/blocks never reached the source.
    assert pm.get_plugin("p1") is p1
    assert pm.get_plugin("p2") is p2
    assert clone.get_plugin("p1") is None
    assert clone.is_blocked("p2")
    assert clone.get_plugin("p3") is not None
    assert not pm.is_registered(clone.get_plugin("p3"))


# --------------------------------------------------------------------------- #
# Historic replay                                                              #
# --------------------------------------------------------------------------- #


def test_new_clone_plugin_replays_inherited_history_in_order() -> None:
    log: list[Any] = []
    pm, _p1, _p2 = _populated_manager(log)
    pm.hook.configure.call_historic(kwargs={"arg": 2})
    clone = pm.snapshot().restore()

    received: list[Any] = []

    class Late:
        @hookimpl
        def configure(self, arg: int) -> None:
            received.append(arg)

    clone.register(Late())
    assert received == [1, 2]


def test_historic_event_delivered_at_most_once_per_plugin() -> None:
    pm = PluginManager("example")
    pm.add_hookspecs(Spec)
    pm.hook.configure.call_historic(kwargs={"arg": 1})

    class Late:
        def __init__(self) -> None:
            self.calls = 0

        @hookimpl
        def configure(self, arg: int) -> None:
            self.calls += 1

    clone = pm.snapshot().restore()
    late = Late()
    clone.register(late, "late")
    assert late.calls == 1
    clone.unregister(late)
    clone.register(late, "late")
    assert late.calls == 1


def test_two_new_plugins_each_replay_full_history() -> None:
    pm = PluginManager("example")
    pm.add_hookspecs(Spec)
    pm.hook.configure.call_historic(kwargs={"arg": 1})
    clone = pm.snapshot().restore()

    class Rec:
        def __init__(self, seen: list[Any]) -> None:
            self.seen = seen

        @hookimpl
        def configure(self, arg: int) -> None:
            self.seen.append(arg)

    a_seen: list[Any] = []
    b_seen: list[Any] = []
    clone.register(Rec(a_seen), "a")
    clone.register(Rec(b_seen), "b")
    assert a_seen == [1]
    assert b_seen == [1]


def test_plugin_with_two_impls_gets_event_once() -> None:
    pm = PluginManager("example")
    pm.add_hookspecs(Spec)
    pm.hook.configure.call_historic(kwargs={"arg": 1})
    clone = pm.snapshot().restore()

    calls: list[str] = []

    class Duo:
        @hookimpl
        def configure(self, arg: int) -> None:
            calls.append("first")

        @hookimpl(specname="configure")
        def configure_alias(self, arg: int) -> None:
            calls.append("alias")

    clone.register(Duo())
    assert len(calls) == 1


def test_inherited_result_callback_is_not_carried_into_clone() -> None:
    pm = PluginManager("example")
    pm.add_hookspecs(Spec)
    source_results: list[Any] = []
    pm.hook.configure.call_historic(source_results.append, {"arg": 1})

    class Answer:
        @hookimpl
        def configure(self, arg: int) -> int:
            return int(arg)

    pm.register(Answer())
    assert source_results == [1]
    del source_results[:]

    clone = pm.snapshot().restore()

    class Late:
        @hookimpl
        def configure(self, arg: int) -> int:
            return int(arg) + 99

    clone.register(Late())
    # The source-scoped callback must not fire from clone replay.
    assert source_results == []


def test_clone_new_historic_event_stays_in_clone() -> None:
    log: list[Any] = []
    pm, _p1, _p2 = _populated_manager(log)
    clone = pm.snapshot().restore()
    clone_b = pm.snapshot().restore()

    clone.hook.configure.call_historic(kwargs={"arg": 9})
    # Fork plugins currently registered in the clone receive the new event.
    assert ("p1-conf", 9) in log
    assert ("p2-conf", 9) in log
    log.clear()

    class Late:
        @hookimpl
        def configure(self, arg: int) -> None:
            log.append(("late", arg))

    late = Late()
    # The source manager never heard event 9: a late plugin there only
    # receives the source's own event (1).
    pm.register(late, "late")
    assert ("late", 1) in log
    assert ("late", 9) not in log
    # Neither does an independent clone.
    late_b = Late()
    clone_b.register(late_b, "late_b")
    assert ("late", 9) not in log
    # But a plugin joining the clone receives both inherited (1) and new (9).
    late_c = Late()
    clone.register(late_c, "late_c")
    assert log.count(("late", 1)) == 3
    assert log.count(("late", 9)) == 1


def test_clone_originated_callbacks_fire_for_late_plugins() -> None:
    pm = PluginManager("example")
    pm.add_hookspecs(Spec)
    clone = pm.snapshot().restore()

    class Answer:
        @hookimpl
        def configure(self, arg: int) -> int:
            return int(arg) * 2

    clone.hook.configure.call_historic(kwargs={"arg": 21})
    results: list[Any] = []
    clone.hook.configure.call_historic(results.append, {"arg": 5})
    # A later plugin replays both clone events; only the second has a
    # callback, and it receives that plugin's result.
    clone.register(Answer(), "answer")
    assert results == [10]


# --------------------------------------------------------------------------- #
# Post-fork operational isolation                                              #
# --------------------------------------------------------------------------- #


def test_register_unregister_isolation() -> None:
    log: list[Any] = []
    pm, p1, _p2 = _populated_manager(log)
    clone = pm.snapshot().restore()

    class Plugin3:
        @hookimpl
        def he_method1(self, arg: int) -> int:
            return int(arg) + 100

    p3 = Plugin3()
    clone.register(p3, "p3")
    assert clone.has_plugin("p3")
    assert not pm.has_plugin("p3")

    clone.unregister(p1)
    assert not clone.has_plugin("p1")
    assert pm.has_plugin("p1")

    class Plugin4:
        @hookimpl
        def he_method1(self, arg: int) -> int:
            return int(arg) - 1

    p4 = Plugin4()
    pm.register(p4, "p4")
    assert pm.has_plugin("p4")
    assert not clone.has_plugin("p4")

    assert clone.hook.he_method1(arg=0) == [100, 10]
    assert pm.hook.he_method1(arg=0) == [1, -1, 10]


def test_hookspec_addition_and_conflict_are_isolated() -> None:
    log: list[Any] = []
    pm, _p1, _p2 = _populated_manager(log)
    clone = pm.snapshot().restore()

    class MoreSpecs:
        @hookspec
        def new_hook(self, arg: int) -> None: ...

    clone.add_hookspecs(MoreSpecs)
    assert hasattr(clone.hook, "new_hook")
    assert not hasattr(pm.hook, "new_hook")

    # Re-adding the same spec to the clone is a conflict error that does not
    # touch the source, which is still free to add the spec itself.
    with pytest.raises(ValueError, match="already registered"):
        clone.add_hookspecs(MoreSpecs)
    pm.add_hookspecs(MoreSpecs)
    assert hasattr(pm.hook, "new_hook")


def test_block_unblock_isolation() -> None:
    log: list[Any] = []
    pm, p1, _p2 = _populated_manager(log)
    clone = pm.snapshot().restore()

    clone.set_blocked("p1")
    assert clone.is_blocked("p1")
    assert not clone.has_plugin("p1")
    assert pm.has_plugin("p1")
    assert not pm.is_blocked("p1")
    # Blocking in the clone keeps a later registration out.
    assert clone.register(Plugin1(), "p1") is None

    pm.set_blocked("p2")
    assert pm.is_blocked("p2")
    assert clone.has_plugin("p2")

    assert clone.unblock("p1") is True
    assert clone.register(p1, "p1") == "p1"
    assert pm.is_registered(p1)


def test_check_pending_isolation() -> None:
    log: list[Any] = []
    pm, _p1, _p2 = _populated_manager(log)
    clone = pm.snapshot().restore()
    pm.check_pending()
    clone.check_pending()

    class UnknownHookPlugin:
        @hookimpl
        def a_hook_without_spec(self, arg: int) -> None: ...

    clone.register(UnknownHookPlugin())
    with pytest.raises(Exception, match="unknown hook"):
        clone.check_pending()
    pm.check_pending()  # the source never saw the clone's unknown hook


def test_hook_impl_ordering_isolation() -> None:
    log: list[Any] = []
    pm, _p1, _p2 = _populated_manager(log)
    clone = pm.snapshot().restore()
    source_order = [i.plugin_name for i in pm.hook.he_method1.get_hookimpls()]
    assert [
        i.plugin_name for i in clone.hook.he_method1.get_hookimpls()
    ] == source_order

    class Last:
        @hookimpl(trylast=True)
        def he_method1(self, arg: int) -> int:
            return 500

    clone.register(Last(), "last")
    assert [i.plugin_name for i in clone.hook.he_method1.get_hookimpls()] == [
        "last",
        "p2",
        "p1",
    ]
    assert [i.plugin_name for i in pm.hook.he_method1.get_hookimpls()] == source_order
    assert clone.hook.he_method1(arg=0) == [1, 10, 500]


# --------------------------------------------------------------------------- #
# Call semantics preserved in clones                                           #
# --------------------------------------------------------------------------- #


def test_normal_call_matches_source() -> None:
    log: list[Any] = []
    pm, _p1, _p2 = _populated_manager(log)
    clone = pm.snapshot().restore()
    assert clone.hook.he_method1(arg=3) == pm.hook.he_method1(arg=3) == [4, 13]


def test_firstresult_semantics() -> None:
    pm = PluginManager("example")
    pm.add_hookspecs(FirstResultSpec)

    class NoneReturner:
        @hookimpl
        def first(self, arg: int) -> None:
            return None

    class Answer:
        @hookimpl
        def first(self, arg: int) -> int:
            return int(arg) + 1

    pm.register(NoneReturner())
    pm.register(Answer())
    clone = pm.snapshot().restore()
    assert clone.hook.first(arg=41) == 42
    assert pm.hook.first(arg=41) == 42


def test_wrapper_and_old_style_hookwrapper_and_result() -> None:
    class Api:
        @hookspec
        def hello(self, arg: int) -> object: ...

    class Plain:
        @hookimpl
        def hello(self, arg: int) -> int:
            return int(arg) + 1

    class NewWrapper:
        @hookimpl(wrapper=True)
        def hello(self, arg: int):
            result = yield
            return [v + 100 for v in result]

    class OldWrapper:
        @hookimpl(hookwrapper=True)
        def hello(self, arg: int):
            outcome = yield
            outcome.force_result(["forced"])

    pm = PluginManager("example")
    pm.add_hookspecs(Api)
    pm.register(Plain())
    pm.register(NewWrapper())
    clone = pm.snapshot().restore()
    # New-style wrapper transforms the result identically in the clone.
    assert clone.hook.hello(arg=1) == pm.hook.hello(arg=1) == [102]

    clone.register(OldWrapper())
    pm.register(OldWrapper())
    # The old-style wrapper forces the result; semantics match the source.
    assert clone.hook.hello(arg=1) == pm.hook.hello(arg=1) == ["forced"]


def test_call_extra_in_clone() -> None:
    log: list[Any] = []
    pm, _p1, _p2 = _populated_manager(log)
    clone = pm.snapshot().restore()

    def extra(arg: int) -> int:
        return int(arg) * 100

    # The temporary impl is inserted after the last non-tryfirst regular
    # impl, so reverse invocation order is p1(tryfirst), extra, p2.
    assert clone.hook.he_method1.call_extra([extra], {"arg": 1}) == [2, 100, 11]
    # call_extra is temporary: the extra method is not registered.
    assert clone.hook.he_method1(arg=1) == [2, 11]
    assert len(clone.hook.he_method1.get_hookimpls()) == 2


def test_subset_hook_caller_in_clone() -> None:
    log: list[Any] = []
    pm, p1, _p2 = _populated_manager(log)
    clone = pm.snapshot().restore()
    subset = clone.subset_hook_caller("he_method1", [p1])
    assert subset(arg=0) == [10]
    # The full caller keeps both implementations.
    assert clone.hook.he_method1(arg=0) == [1, 10]


def test_plugin_exception_propagates_in_clone() -> None:
    class Api:
        @hookspec
        def boom(self) -> None: ...

    class Explodes:
        @hookimpl
        def boom(self) -> None:
            raise ValueError("kaboom")

    pm = PluginManager("example")
    pm.add_hookspecs(Api)
    pm.register(Explodes())
    clone = pm.snapshot().restore()
    with pytest.raises(ValueError, match="kaboom"):
        clone.hook.boom()


def test_monitoring_is_independent() -> None:
    log: list[Any] = []
    pm, _p1, _p2 = _populated_manager(log)
    source_events: list[Any] = []

    def before(hook_name: str, hook_impls: Any, kwargs: Any) -> None:
        source_events.append(("before", hook_name))

    def after(outcome: Any, hook_name: str, hook_impls: Any, kwargs: Any) -> None:
        source_events.append(("after", hook_name))

    undo_source = pm.add_hookcall_monitoring(before, after)
    clone = pm.snapshot().restore()

    clone_events: list[Any] = []
    undo_clone = clone.add_hookcall_monitoring(
        lambda n, i, k: clone_events.append(("before", n)),
        lambda o, n, i, k: clone_events.append(("after", n)),
    )

    clone.hook.he_method1(arg=0)
    assert [e[1] for e in clone_events if e[0] == "before"] == ["he_method1"]
    assert source_events == []

    pm.hook.he_method1(arg=0)
    assert [e[1] for e in source_events if e[0] == "before"] == ["he_method1"]
    assert len(clone_events) == 2

    undo_clone()
    clone.hook.he_method1(arg=0)
    assert len(clone_events) == 2
    undo_source()


def test_tracing_is_independent() -> None:
    log: list[Any] = []
    pm, _p1, _p2 = _populated_manager(log)
    clone = pm.snapshot().restore()
    # Each manager owns its own tracer root.
    assert clone.trace.root is not pm.trace.root

    source_out: list[str] = []
    clone_out: list[str] = []
    pm.trace.root.setwriter(source_out.append)
    clone.trace.root.setwriter(clone_out.append)
    undo_pm = pm.enable_tracing()
    undo_clone = clone.enable_tracing()
    try:
        clone.hook.he_method1(arg=0)
        assert any("he_method1" in line for line in clone_out)
        assert source_out == []
        pm.hook.he_method1(arg=0)
        assert any("he_method1" in line for line in source_out)
        assert len(clone_out) == 2
    finally:
        undo_pm()
        undo_clone()


def test_clone_can_be_snapshotted_again() -> None:
    log: list[Any] = []
    pm, _p1, _p2 = _populated_manager(log)
    clone = pm.snapshot().restore()

    class Plugin3:
        @hookimpl
        def he_method1(self, arg: int) -> int:
            return int(arg) + 100

    clone.register(Plugin3(), "p3")
    clone.hook.configure.call_historic(kwargs={"arg": 3})
    fork = clone.snapshot().restore()
    assert fork.has_plugin("p3")
    received: list[Any] = []

    class Late:
        @hookimpl
        def configure(self, arg: int) -> None:
            received.append(arg)

    fork.register(Late())
    # The clone's inherited (1) and own (3) events are all replayed.
    assert received == [1, 3]


# --------------------------------------------------------------------------- #
# Snapshot lifecycle errors                                                    #
# --------------------------------------------------------------------------- #


def test_snapshot_is_single_use() -> None:
    pm = PluginManager("example")
    snap = pm.snapshot()
    clone = snap.restore()
    assert clone is not None
    assert snap.is_consumed
    with pytest.raises(SnapshotReusedError, match="already been restored"):
        snap.restore()


def test_destroyed_snapshot_is_rejected() -> None:
    pm = PluginManager("example")
    snap = pm.snapshot()
    snap.destroy()
    assert snap.is_destroyed
    with pytest.raises(SnapshotDestroyedError, match="destroyed"):
        snap.restore()
    target = PluginManager("example")
    with pytest.raises(SnapshotDestroyedError):
        target.restore_snapshot(snap)


def test_expired_snapshot_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    pm = PluginManager("example")
    snap = pm.snapshot()
    assert not bool(snap.is_expired)
    with monkeypatch.context() as patch:
        patch.setattr(snapshot_module, "_SUPPORTED_FORMAT_VERSIONS", frozenset({999}))
        assert bool(snap.is_expired)
        with pytest.raises(SnapshotExpiredError, match="not supported"):
            snap.restore()
        # The snapshot was not consumed by the failed attempt.
        assert not bool(snap.is_consumed)
    # Once a supported format is available again, the same snapshot restores.
    clone = snap.restore()
    assert isinstance(clone, PluginManager)


def test_cross_project_snapshot_is_rejected() -> None:
    pm = PluginManager("example")
    snap = pm.snapshot()
    other = PluginManager("different")
    with pytest.raises(SnapshotCrossManagerError, match="different"):
        other.restore_snapshot(snap)
    # A rejected restore burns neither the snapshot nor the target.
    assert not snap.is_consumed
    assert other.list_name_plugin() == []
    clone = snap.restore()
    assert clone.project_name == "example"


def test_restore_into_origin_manager_is_rejected() -> None:
    pm = PluginManager("example")
    pm.add_hookspecs(Spec)
    snap = pm.snapshot()
    with pytest.raises(SnapshotCrossManagerError, match="manager that created"):
        pm.restore_snapshot(snap)
    assert not snap.is_consumed
    assert pm.manager_version == 1


def test_restore_into_nonempty_manager_is_rejected() -> None:
    log: list[Any] = []
    pm, _p1, _p2 = _populated_manager(log)
    target = PluginManager("example")
    target.register(Plugin1(), "existing")
    snap = pm.snapshot()
    with pytest.raises(SnapshotError, match="empty PluginManager"):
        target.restore_snapshot(snap)
    assert [n for n, _ in target.list_name_plugin()] == ["existing"]
    assert not snap.is_consumed


def test_invalid_snapshots_fail_before_plugin_callbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log: list[Any] = []
    pm, _p1, _p2 = _populated_manager(log)
    log.clear()

    consumed = pm.snapshot()
    consumed.restore()
    destroyed = pm.snapshot()
    destroyed.destroy()
    expired = pm.snapshot()
    cross = pm.snapshot()
    origin = pm.snapshot()

    for snap, error in [
        (consumed, SnapshotReusedError),
        (destroyed, SnapshotDestroyedError),
        (origin, SnapshotCrossManagerError),
    ]:
        with pytest.raises(error):
            if snap is origin:
                pm.restore_snapshot(snap)
            else:
                snap.restore()
        assert log == []

    with monkeypatch.context() as patch:
        patch.setattr(snapshot_module, "_SUPPORTED_FORMAT_VERSIONS", frozenset({999}))
        with pytest.raises(SnapshotExpiredError):
            expired.restore()
    assert log == []
    assert not expired.is_consumed

    with pytest.raises(SnapshotCrossManagerError):
        PluginManager("other").restore_snapshot(cross)
    assert log == []


def test_snapshot_repr() -> None:
    pm = PluginManager("example")
    snap = pm.snapshot()
    text = repr(snap)
    assert "PluginManagerSnapshot" in text
    assert "'example'" in text
    assert "active" in text
    snap.restore()
    assert "consumed" in repr(snap)
    snap2 = pm.snapshot()
    snap2.destroy()
    assert "destroyed" in repr(snap2)


# --------------------------------------------------------------------------- #
# Backward compatibility                                                       #
# --------------------------------------------------------------------------- #


def test_plain_manager_keeps_legacy_replay_every_time() -> None:
    """Without snapshotting, re-registering a plugin replays all historic
    events every time, as before."""
    pm = PluginManager("example")
    pm.add_hookspecs(Spec)
    pm.hook.configure.call_historic(kwargs={"arg": 1})

    class Counter:
        def __init__(self) -> None:
            self.count = 0

        @hookimpl
        def configure(self, arg: int) -> None:
            self.count += 1

    counter = Counter()
    pm.register(counter, "counter")
    assert counter.count == 1
    pm.unregister(counter)
    pm.register(counter, "counter")
    assert counter.count == 2
    assert pm._historic_replay is None


def test_snapshot_public_api_exports() -> None:
    import pluggy

    assert pluggy.PluginManagerSnapshot is PluginManagerSnapshot
    for exc in (
        SnapshotError,
        SnapshotExpiredError,
        SnapshotCrossManagerError,
        SnapshotReusedError,
        SnapshotDestroyedError,
    ):
        assert issubclass(exc, SnapshotError)
        assert exc.__name__ in pluggy.__all__
