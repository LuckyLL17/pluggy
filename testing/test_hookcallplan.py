import pytest

from pluggy import HookCallError
from pluggy import HookCallPlan
from pluggy import HookCallPlanError
from pluggy import HookimplMarker
from pluggy import HookspecMarker
from pluggy import PluginManager


hookspec = HookspecMarker("example")
hookimpl = HookimplMarker("example")


def _impl_functions(impls):
    return [impl.function for impl in impls]


class Plain1:
    @hookimpl
    def hello(self, arg):
        return ("plain1", arg)


class Plain2:
    @hookimpl
    def hello(self, arg):
        return ("plain2", arg)


class TryFirstPlugin:
    @hookimpl(tryfirst=True)
    def hello(self, arg):
        return ("tryfirst", arg)


class TryLastPlugin:
    @hookimpl(trylast=True)
    def hello(self, arg):
        return ("trylast", arg)


class WrapperPlugin:
    @hookimpl(wrapper=True)
    def hello(self, arg):
        result = yield
        return [("wrapper-pre", arg), *result]


class OldWrapperPlugin:
    @hookimpl(hookwrapper=True)
    def hello(self, arg):
        outcome = yield
        outcome.force_result([("oldwrapper", arg), *outcome.get_result()])


class SpecnamePlugin:
    @hookimpl(specname="hello")
    def aliased(self, arg, optional=0):
        return ("specname", arg, optional)


class NoImplPlugin:
    pass


@pytest.fixture
def pm_with_spec() -> PluginManager:
    class Hooks:
        @hookspec
        def hello(self, arg: int) -> None:
            pass

    pm = PluginManager("example")
    pm.add_hookspecs(Hooks)
    return pm


def test_plan_records_implementations_and_metadata(pm_with_spec: PluginManager) -> None:
    pm = pm_with_spec
    plain1 = Plain1()
    try_first = TryFirstPlugin()
    try_last = TryLastPlugin()
    wrapper = WrapperPlugin()
    old_wrapper = OldWrapperPlugin()
    specname = SpecnamePlugin()
    plugins = [plain1, Plain2(), try_first, try_last, wrapper, old_wrapper, specname]
    for plugin in plugins:
        pm.register(plugin)

    plan = pm.plan_hook_call("hello", plugins)

    assert isinstance(plan, HookCallPlan)
    assert plan.name == "hello"
    assert plan.firstresult is False
    assert plan.plugins == tuple(plugins)

    expected = pm.hook.hello.get_hookimpls()
    assert _impl_functions(plan.get_hookimpls()) == _impl_functions(expected)
    assert plan.get_hookimpls() == list(plan.hookimpls)
    assert isinstance(plan.hookimpls, tuple)

    plain1_impl = next(impl for impl in plan.hookimpls if impl.plugin is plain1)
    assert plain1_impl.plugin is plain1
    assert plain1_impl.plugin_name == pm.get_name(plain1)
    assert plain1_impl.opts["specname"] is None
    assert plain1_impl.tryfirst is False
    assert plain1_impl.trylast is False
    assert plain1_impl.wrapper is False
    assert plain1_impl.hookwrapper is False
    assert plain1_impl.argnames == ("arg",)

    first_impl = next(impl for impl in plan.hookimpls if impl.plugin is try_first)
    assert first_impl.tryfirst is True
    last_impl = next(impl for impl in plan.hookimpls if impl.plugin is try_last)
    assert last_impl.trylast is True

    wrapper_impl = next(impl for impl in plan.hookimpls if impl.plugin is wrapper)
    assert wrapper_impl.wrapper is True
    assert wrapper_impl.hookwrapper is False

    old_wrapper_impl = next(
        impl for impl in plan.hookimpls if impl.plugin is old_wrapper
    )
    assert old_wrapper_impl.hookwrapper is True
    assert old_wrapper_impl.wrapper is False

    aliased_impl = next(impl for impl in plan.hookimpls if impl.plugin is specname)
    assert aliased_impl.opts["specname"] == "hello"
    assert aliased_impl.argnames == ("arg",)
    assert aliased_impl.kwargnames == ("optional",)

    # All wrappers are recorded after the regular implementations, matching
    # HookCaller ordering (the multicall iterates the list in reverse).
    kinds = [impl.wrapper or impl.hookwrapper for impl in plan.hookimpls]
    assert kinds == sorted(kinds)
    assert kinds.count(True) == 2


def test_plan_records_firstresult(pm: PluginManager) -> None:
    class Hooks:
        @hookspec(firstresult=True)
        def hello(self, arg: int) -> None:
            pass

    pm.add_hookspecs(Hooks)
    plugin = Plain1()
    pm.register(plugin)
    plan = pm.plan_hook_call("hello", [plugin])
    assert plan.firstresult is True


def test_plan_creation_does_not_call_callbacks(pm_with_spec: PluginManager) -> None:
    pm = pm_with_spec
    calls: list[str] = []

    class P:
        @hookimpl(wrapper=True)
        def hello(self, arg):
            calls.append("wrapper-setup")
            yield
            calls.append("wrapper-teardown")

    pm.register(P())
    pm.plan_hook_call("hello", list(pm.get_plugins()))
    assert calls == []


def test_plan_creation_does_not_modify_registry(pm_with_spec: PluginManager) -> None:
    pm = pm_with_spec
    plugins = [Plain1(), Plain2(), WrapperPlugin()]
    for plugin in plugins:
        pm.register(plugin)

    registered_before = pm.get_plugins()
    funcs_before = _impl_functions(pm.hook.hello.get_hookimpls())

    plan = pm.plan_hook_call("hello", [plugins[0], plugins[2]])

    assert pm.get_plugins() == registered_before
    assert _impl_functions(pm.hook.hello.get_hookimpls()) == funcs_before
    assert plan.plugins == (plugins[0], plugins[2])


def test_plan_execute_uses_planned_set_and_order(pm_with_spec: PluginManager) -> None:
    pm = pm_with_spec
    plain1, plain2, try_first, try_last = (
        Plain1(),
        Plain2(),
        TryFirstPlugin(),
        TryLastPlugin(),
    )
    for plugin in [plain1, plain2, try_first, try_last]:
        pm.register(plugin)

    plan = pm.plan_hook_call("hello", [plain1, try_first])
    assert plan(arg=5) == [("tryfirst", 5), ("plain1", 5)]
    assert plan.execute(arg=6) == [("tryfirst", 6), ("plain1", 6)]


def test_plan_matches_subset_hook_caller_semantics(
    pm_with_spec: PluginManager,
) -> None:
    pm = pm_with_spec
    plain1, plain2, try_first = Plain1(), Plain2(), TryFirstPlugin()
    for plugin in [plain1, plain2, try_first]:
        pm.register(plugin)

    plan = pm.plan_hook_call("hello", [plain1, try_first])
    subset = pm.subset_hook_caller("hello", remove_plugins=[plain2])
    assert plan(arg=1) == subset(arg=1)


def test_plan_repeated_execution_ignores_newly_registered_plugins(
    pm_with_spec: PluginManager,
) -> None:
    pm = pm_with_spec
    plain1 = Plain1()
    pm.register(plain1)
    plan = pm.plan_hook_call("hello", [plain1])

    assert plan(arg=0) == [("plain1", 0)]

    pm.register(Plain2())
    assert plan(arg=1) == [("plain1", 1)]
    assert plan(arg=2) == [("plain1", 2)]
    assert plan.is_valid()

    # The late plugin still participates in ordinary calls.
    assert pm.hook.hello(arg=3) == [("plain2", 3), ("plain1", 3)]


def test_plan_unknown_hook_raises(pm: PluginManager) -> None:
    plugin = NoImplPlugin()
    pm.register(plugin)
    with pytest.raises(HookCallPlanError, match="unknown hook 'missing'"):
        pm.plan_hook_call("missing", [plugin])


def test_plan_unregistered_plugin_raises(pm_with_spec: PluginManager) -> None:
    with pytest.raises(HookCallPlanError, match="is not registered"):
        pm_with_spec.plan_hook_call("hello", [Plain1()])


def test_plan_duplicate_plugin_raises(pm_with_spec: PluginManager) -> None:
    pm = pm_with_spec
    plugin = Plain1()
    pm.register(plugin)
    with pytest.raises(HookCallPlanError, match="more than once"):
        pm.plan_hook_call("hello", [plugin, plugin])


def test_plan_historic_hook_raises(pm: PluginManager) -> None:
    class Hooks:
        @hookspec(historic=True)
        def hello(self, arg):
            pass

    class P:
        @hookimpl
        def hello(self, arg):
            pass

    pm.add_hookspecs(Hooks)
    pm.register(P())
    with pytest.raises(HookCallPlanError, match="historic"):
        pm.plan_hook_call("hello", list(pm.get_plugins()))


def test_plan_empty_plugin_selection(pm_with_spec: PluginManager) -> None:
    pm = pm_with_spec
    pm.register(Plain1())
    plan = pm.plan_hook_call("hello", [])
    assert plan.hookimpls == ()
    assert plan(arg=0) == []


def test_plan_firstresult_empty_selection_returns_none(pm: PluginManager) -> None:
    class Hooks:
        @hookspec(firstresult=True)
        def hello(self):
            pass

    pm.add_hookspecs(Hooks)
    plan = pm.plan_hook_call("hello", [])
    assert plan() is None


def test_plan_allows_plugin_without_hook_impl(pm_with_spec: PluginManager) -> None:
    pm = pm_with_spec
    silent = NoImplPlugin()
    plain1 = Plain1()
    pm.register(silent)
    pm.register(plain1)
    plan = pm.plan_hook_call("hello", [silent, plain1])
    assert [impl.plugin for impl in plan.hookimpls] == [plain1]
    assert plan(arg=1) == [("plain1", 1)]


def test_plan_covers_multiple_impls_from_one_plugin(
    pm_with_spec: PluginManager,
) -> None:
    pm = pm_with_spec

    class Multi:
        @hookimpl
        def hello(self, arg):
            return ("direct", arg)

        @hookimpl(specname="hello")
        def aliased(self, arg):
            return ("aliased", arg)

        @hookimpl(wrapper=True, specname="hello")
        def around(self, arg):
            result = yield
            return [("around", arg), *result]

    plugin = Multi()
    pm.register(plugin)
    plan = pm.plan_hook_call("hello", [plugin])

    impls = plan.get_hookimpls()
    assert len(impls) == 3
    assert {impl.function.__name__ for impl in impls} == {
        "hello",
        "aliased",
        "around",
    }
    assert all(impl.plugin is plugin for impl in impls)
    assert plan(arg=2) == [
        ("around", 2),
        ("direct", 2),
        ("aliased", 2),
    ]


def test_plan_invalidated_by_unregister_before_execution(
    pm_with_spec: PluginManager,
) -> None:
    pm = pm_with_spec
    calls: list[str] = []

    class P:
        @hookimpl(wrapper=True)
        def hello(self, arg):
            calls.append("wrapper")
            yield

    plugin = P()
    pm.register(plugin)
    plan = pm.plan_hook_call("hello", [plugin])

    pm.unregister(plugin)
    assert plan.is_valid() is False
    reason = plan.invalidation_reason()
    assert reason is not None
    assert "unregistered" in reason

    with pytest.raises(HookCallPlanError, match="unregistered"):
        plan(arg=0)
    # No callback may run when the plan is invalid.
    assert calls == []


def test_plan_invalidated_by_registration_name_change(
    pm_with_spec: PluginManager,
) -> None:
    pm = pm_with_spec
    plugin = Plain1()
    pm.register(plugin)
    plan = pm.plan_hook_call("hello", [plugin])

    pm.unregister(plugin)
    pm.register(plugin, name="renamed")
    reason = plan.invalidation_reason()
    assert reason is not None
    assert "renamed" in reason
    with pytest.raises(HookCallPlanError):
        plan(arg=0)


def test_plan_invalidated_by_firstresult_change(pm: PluginManager) -> None:
    # Register implementations before the spec exists, so firstresult can
    # still go from False (no spec) to True via a later add_hookspecs().
    plugin = Plain1()
    pm.register(plugin)
    plan = pm.plan_hook_call("hello", [plugin])
    assert plan.firstresult is False

    class Hooks:
        @hookspec(firstresult=True)
        def hello(self, arg):
            pass

    pm.add_hookspecs(Hooks)
    reason = plan.invalidation_reason()
    assert reason is not None
    assert "firstresult" in reason
    with pytest.raises(HookCallPlanError, match="firstresult"):
        plan(arg=0)


def test_plan_valid_when_spec_added_without_firstresult_change(
    pm: PluginManager,
) -> None:
    plugin = Plain1()
    pm.register(plugin)
    plan = pm.plan_hook_call("hello", [plugin])

    class Hooks:
        @hookspec(firstresult=False)
        def hello(self, arg):
            pass

    pm.add_hookspecs(Hooks)
    assert plan.is_valid()
    assert plan(arg=7) == [("plain1", 7)]


def test_plan_invalidated_by_order_change(pm_with_spec: PluginManager) -> None:
    pm = pm_with_spec
    plain1, plain2 = Plain1(), Plain2()
    pm.register(plain1)
    pm.register(plain2)
    plan = pm.plan_hook_call("hello", [plain1, plain2])
    assert [impl.plugin for impl in plan.hookimpls] == [plain1, plain2]

    # Re-registering in the opposite order reverses the call order while
    # keeping both plugins and all options unchanged.
    pm.unregister(plain1)
    pm.unregister(plain2)
    pm.register(plain2)
    pm.register(plain1)

    reason = plan.invalidation_reason()
    assert reason is not None
    assert "position" in reason
    with pytest.raises(HookCallPlanError, match="position"):
        plan(arg=0)


def test_plan_invalidated_by_tryfirst_change(pm_with_spec: PluginManager) -> None:
    pm = pm_with_spec
    plugin = Plain1()
    pm.register(plugin)
    plan = pm.plan_hook_call("hello", [plugin])

    Plain1.hello.example_impl["tryfirst"] = True
    try:
        pm.unregister(plugin)
        pm.register(plugin)
        reason = plan.invalidation_reason()
        assert reason is not None
        assert "tryfirst" in reason
        assert "False" in reason and "True" in reason
        with pytest.raises(HookCallPlanError, match="tryfirst"):
            plan(arg=0)
    finally:
        Plain1.hello.example_impl["tryfirst"] = False


def test_plan_executes_new_style_and_old_style_wrappers(
    pm_with_spec: PluginManager,
) -> None:
    pm = pm_with_spec
    plain1, wrapper, old_wrapper = (
        Plain1(),
        WrapperPlugin(),
        OldWrapperPlugin(),
    )
    for plugin in [plain1, wrapper, old_wrapper]:
        pm.register(plugin)

    plan = pm.plan_hook_call("hello", [plain1, wrapper, old_wrapper])
    # The old-style wrapper is outermost (recorded after the new-style
    # wrapper), so its forced result wraps everything produced inside.
    assert plan(arg=9) == [
        ("oldwrapper", 9),
        ("wrapper-pre", 9),
        ("plain1", 9),
    ]


def test_plan_propagates_exception_through_wrappers(
    pm_with_spec: PluginManager,
) -> None:
    pm = pm_with_spec
    seen: list[BaseException | None] = []

    class Failing:
        @hookimpl
        def hello(self, arg):
            raise RuntimeError("boom")

    class Observing:
        @hookimpl(hookwrapper=True)
        def hello(self, arg):
            outcome = yield
            seen.append(outcome.exception)

    failing, observing = Failing(), Observing()
    pm.register(failing)
    pm.register(observing)
    plan = pm.plan_hook_call("hello", [failing, observing])

    with pytest.raises(RuntimeError, match="boom"):
        plan(arg=0)
    assert isinstance(seen[0], RuntimeError)


def test_plan_wrapper_can_suppress_exception(pm_with_spec: PluginManager) -> None:
    pm = pm_with_spec

    class Failing:
        @hookimpl
        def hello(self, arg):
            raise RuntimeError("boom")

    class Suppressing:
        @hookimpl(wrapper=True)
        def hello(self, arg):
            try:
                yield
            except RuntimeError as exc:
                return f"caught: {exc}"

    pm.register(Failing())
    pm.register(Suppressing())
    plan = pm.plan_hook_call("hello", list(pm.get_plugins()))
    assert plan(arg=0) == "caught: boom"


def test_plan_firstresult_short_circuits(pm: PluginManager) -> None:
    calls: list[str] = []

    class Hooks:
        @hookspec(firstresult=True)
        def hello(self, arg):
            pass

    class NonePlugin:
        @hookimpl
        def hello(self, arg):
            calls.append("none")
            return None

    class TwoPlugin:
        @hookimpl
        def hello(self, arg):
            calls.append("two")
            return 2

    class ThreePlugin:
        @hookimpl
        def hello(self, arg):
            calls.append("three")
            return 3

    class Wrapping:
        @hookimpl(wrapper=True)
        def hello(self, arg):
            result = yield
            calls.append(f"wrapper saw {result}")
            return result

    none_plugin, two_plugin, three_plugin, wrapping = (
        NonePlugin(),
        TwoPlugin(),
        ThreePlugin(),
        Wrapping(),
    )
    pm.add_hookspecs(Hooks)
    # The multicall runs regular implementations in reverse registration
    # order; register them in the reverse of the intended execution order.
    for plugin in [three_plugin, two_plugin, none_plugin, wrapping]:
        pm.register(plugin)

    plan = pm.plan_hook_call(
        "hello", [none_plugin, two_plugin, three_plugin, wrapping]
    )
    assert plan.firstresult is True
    assert plan(arg=0) == 2
    assert calls == ["none", "two", "wrapper saw 2"]


def test_plan_hookimpls_survive_unregister(pm_with_spec: PluginManager) -> None:
    pm = pm_with_spec
    plugin = Plain1()
    pm.register(plugin)
    plan = pm.plan_hook_call("hello", [plugin])
    frozen = plan.get_hookimpls()

    pm.unregister(plugin)
    assert pm.hook.hello.get_hookimpls() == []
    assert len(plan.hookimpls) == 1
    assert plan.get_hookimpls() == frozen
    assert plan.hookimpls[0].plugin is plugin


def test_plan_is_immutable(pm_with_spec: PluginManager) -> None:
    pm = pm_with_spec
    plugin = Plain1()
    pm.register(plugin)
    plan = pm.plan_hook_call("hello", [plugin])

    with pytest.raises(AttributeError):
        plan.name = "other"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        plan.firstresult = True  # type: ignore[misc]
    with pytest.raises(AttributeError):
        plan.hookimpls = ()  # type: ignore[misc]
    with pytest.raises(TypeError):
        plan.hookimpls[0] = None  # type: ignore[index]


def test_plan_kwargs_verified_like_hookcaller(pm_with_spec: PluginManager) -> None:
    pm = pm_with_spec
    plugin = Plain1()
    pm.register(plugin)
    plan = pm.plan_hook_call("hello", [plugin])

    with pytest.raises(HookCallError), pytest.warns(UserWarning):
        plan()

    with pytest.raises(HookCallError), pytest.warns(UserWarning):
        plan.execute()


def test_plan_works_with_hook_monitoring(pm_with_spec: PluginManager) -> None:
    pm = pm_with_spec
    plain1, plain2, excluded = Plain1(), Plain2(), TryFirstPlugin()
    for plugin in [plain1, plain2, excluded]:
        pm.register(plugin)
    plan = pm.plan_hook_call("hello", [plain1, plain2])

    seen = []

    def before(hook_name, hook_impls, kwargs):
        seen.append((hook_name, len(hook_impls), kwargs["arg"]))

    def after(outcome, hook_name, hook_impls, kwargs):
        seen.append((hook_name, len(hook_impls), outcome.get_result()))

    undo = pm.add_hookcall_monitoring(before, after)
    try:
        assert plan(arg=4) == [("plain2", 4), ("plain1", 4)]
    finally:
        undo()

    assert seen == [
        ("hello", 2, 4),
        ("hello", 2, [("plain2", 4), ("plain1", 4)]),
    ]
    # Monitoring and ordinary calls are unaffected by the existence of a plan.
    assert pm.hook.hello(arg=5) == [
        ("tryfirst", 5),
        ("plain2", 5),
        ("plain1", 5),
    ]


def test_plan_repr(pm_with_spec: PluginManager) -> None:
    pm = pm_with_spec
    pm.register(Plain1())
    plan = pm.plan_hook_call("hello", list(pm.get_plugins()))
    assert repr(plan) == "<HookCallPlan 'hello', 1 implementation(s)>"


def test_plan_and_regular_calls_coexist(pm_with_spec: PluginManager) -> None:
    pm = pm_with_spec
    plain1, plain2 = Plain1(), Plain2()
    pm.register(plain1)
    pm.register(plain2)

    plan = pm.plan_hook_call("hello", [plain1])

    # Regular calls and call_extra remain unaffected.
    assert pm.hook.hello(arg=0) == [("plain2", 0), ("plain1", 0)]

    def extra(arg):
        return ("extra", arg)

    assert pm.hook.hello.call_extra([extra], {"arg": 0}) == [
        ("extra", 0),
        ("plain2", 0),
        ("plain1", 0),
    ]

    # Subset callers remain unaffected.
    assert pm.subset_hook_caller("hello", [plain2])(arg=0) == [("plain1", 0)]

    # And the plan still does what it promised.
    assert plan(arg=1) == [("plain1", 1)]
