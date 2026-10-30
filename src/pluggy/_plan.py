"""
Immutable hook call plans.

A :class:`HookCallPlan` freezes the set of implementations (and their
ordering) which participate in one particular hook call, ahead of the call
itself.  Creating or inspecting a plan never calls a plugin callback and never
changes the plugin registry.  Executing the plan runs exactly the frozen
implementations through the regular multicall machinery, and refuses to run
anything if the registry changed in a way that affects the plan.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final
from typing import final
from typing import TYPE_CHECKING

from ._caller import HookCaller
from ._impl import _Plugin
from ._impl import HookImpl


if TYPE_CHECKING:
    from ._manager import PluginManager


class HookCallPlanError(Exception):
    """A :class:`HookCallPlan` could not be created, or is no longer valid.

    Raised when a plan is requested for an unknown hook, with an
    unregistered or duplicated plugin, or when an (otherwise immutable) plan
    is executed after the registered implementations, their ordering or the
    hook's ``firstresult`` status changed.

    .. versionadded:: 1.8
    """


_UNSET: Final = object()


def _hook_firstresult(hook: HookCaller) -> bool:
    return hook.spec.opts.get("firstresult", False) if hook.spec is not None else False


def _same_plugin(left: _Plugin, right: _Plugin) -> bool:
    return left is right or left == right


@final
class HookCallPlan:
    """An immutable plan of one hook call on a fixed set of plugins.

    Create plans with :meth:`PluginManager.plan_hook_call`.  A plan records
    the hook :attr:`name`, the hook specification's :attr:`firstresult`
    status, the selected :attr:`plugins` and the participating
    :class:`~pluggy.HookImpl` objects in their final call order
    (:attr:`hookimpls`).  Each recorded implementation exposes its owning
    plugin (``HookImpl.plugin``/``HookImpl.plugin_name``), ``specname`` and
    ordering flags through ``HookImpl.opts``/``HookImpl.tryfirst``/
    ``HookImpl.trylast`` and a summary of the parameter names it requests
    through ``HookImpl.argnames``/``HookImpl.kwargnames``.

    Neither creating nor inspecting a plan executes plugin callbacks or
    modifies the plugin registry.  Calling the plan (``plan(**kwargs)`` or
    :meth:`execute`) validates the plan first and raises
    :class:`HookCallPlanError` *before* any callback runs if a selected
    plugin was unregistered, the implementations or their ordering changed,
    or the hook's ``firstresult`` status changed.  Plugins registered after
    the plan was created are never picked up.

    .. versionadded:: 1.8
    """

    __slots__ = (
        "_impl_opts",
        "_manager",
        "_plugin_names",
        "firstresult",
        "hookimpls",
        "name",
        "plugins",
    )

    def __init__(
        self,
        manager: PluginManager,
        name: str,
        plugins: Iterable[_Plugin],
    ) -> None:
        """:meta private: use :meth:`PluginManager.plan_hook_call`."""
        hook = self._lookup_hook(manager, name)
        if hook is None:
            raise HookCallPlanError(f"unknown hook {name!r}")
        if hook.is_historic():
            raise HookCallPlanError(
                f"cannot create a call plan for the historic hook {name!r}, "
                "use call_historic instead"
            )

        selected_plugins: list[_Plugin] = []
        selected_names: list[str] = []
        for plugin in plugins:
            for already in selected_plugins:
                if _same_plugin(plugin, already):
                    raise HookCallPlanError(
                        f"plugin {plugin!r} is selected more than once for "
                        f"hook {name!r}"
                    )
            plugin_name = manager.get_name(plugin)
            if plugin_name is None:
                raise HookCallPlanError(
                    f"plugin {plugin!r} is not registered, cannot include it "
                    f"in a call plan for hook {name!r}"
                )
            selected_plugins.append(plugin)
            selected_names.append(plugin_name)

        selected = tuple(selected_plugins)
        selected_impls = tuple(
            impl for impl in hook._hookimpls if impl.plugin in selected
        )
        #: The name of the planned hook.
        self.name: Final = name
        #: The plugins the plan was created with, in the given order.
        self.plugins: Final[tuple[_Plugin, ...]] = selected
        self._plugin_names: Final[tuple[str, ...]] = tuple(selected_names)
        #: Whether the hook was marked ``firstresult`` when the plan was made.
        self.firstresult: Final = _hook_firstresult(hook)
        #: Participating implementations in final call order (the multicall
        #: iterates this sequence in reverse, as it does for a regular call).
        self.hookimpls: Final[tuple[HookImpl, ...]] = selected_impls
        # Private copies of the impl options: HookImpl.opts aliases the
        # mutable marker attribute dict on the function, so snapshot the
        # values to detect changes made after the plan was created.
        self._impl_opts: Final[tuple[dict[str, object], ...]] = tuple(
            dict(impl.opts) for impl in selected_impls
        )
        self._manager: Final[PluginManager] = manager

    def __setattr__(self, name: str, value: object) -> None:
        already_set = getattr(self, name, _UNSET) is not _UNSET
        if already_set or name not in type(self).__slots__:
            raise AttributeError(
                f"{type(self).__name__!r} object attribute {name!r} is read-only"
            )
        object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        raise AttributeError(
            f"{type(self).__name__!r} object attribute {name!r} is read-only"
        )

    @staticmethod
    def _lookup_hook(manager: PluginManager, name: str) -> HookCaller | None:
        hook = getattr(manager.hook, name, None)
        return hook if isinstance(hook, HookCaller) else None

    def get_hookimpls(self) -> list[HookImpl]:
        """Return the planned hook implementations in their call order."""
        return list(self.hookimpls)

    def invalidation_reason(self) -> str | None:
        """Return why the plan must not be executed, or ``None`` if valid.

        Only inspects registry state; never executes plugin callbacks.
        """
        hook = self._lookup_hook(self._manager, self.name)
        if hook is None:
            return f"hook {self.name!r} no longer exists"

        for plugin, plugin_name in zip(self.plugins, self._plugin_names):
            current_name = self._manager.get_name(plugin)
            if current_name is None:
                return (
                    f"plugin {plugin_name!r} ({plugin!r}) selected for hook "
                    f"{self.name!r} has been unregistered"
                )
            if current_name != plugin_name:
                return (
                    f"plugin {plugin!r} selected for hook {self.name!r} was "
                    f"planned under the name {plugin_name!r} but is now "
                    f"registered as {current_name!r}"
                )

        current_firstresult = _hook_firstresult(hook)
        if current_firstresult != self.firstresult:
            return (
                f"firstresult status of hook {self.name!r} changed from "
                f"{self.firstresult} to {current_firstresult} after the call "
                "plan was created"
            )

        current_impls = [
            impl for impl in hook._hookimpls if impl.plugin in self.plugins
        ]
        if len(current_impls) != len(self.hookimpls):
            return (
                f"the implementations of hook {self.name!r} provided by the "
                f"selected plugins changed: the plan recorded "
                f"{len(self.hookimpls)} but there are now "
                f"{len(current_impls)}"
            )

        for position, (planned, actual) in enumerate(zip(self.hookimpls, current_impls)):
            if actual.function != planned.function:
                return (
                    f"the implementation at call position {position} of hook "
                    f"{self.name!r} changed: planned {planned.function!r}, "
                    f"found {actual.function!r}"
                )
            planned_opts = self._impl_opts[position]
            for key, planned_value in planned_opts.items():
                actual_value = actual.opts.get(key)
                if actual_value != planned_value:
                    return (
                        f"hookimpl option {key!r} of {planned.function!r} for "
                        f"hook {self.name!r} changed from {planned_value!r} to "
                        f"{actual_value!r} after the call plan was created"
                    )

        return None

    def is_valid(self) -> bool:
        """Return whether executing the plan would use the planned call.

        See :meth:`invalidation_reason` for the cause when this is ``False``.
        """
        return self.invalidation_reason() is None

    def execute(self, **kwargs: object) -> object:
        """Execute the planned hook call with the given keyword arguments.

        Raises :class:`HookCallPlanError` before calling any plugin code if
        the plan is no longer valid.  Otherwise the call uses the frozen
        implementations and ordering together with the regular wrapper,
        old-style wrapper, exception propagation, result override and
        firstresult semantics.
        """
        reason = self.invalidation_reason()
        if reason is not None:
            raise HookCallPlanError(reason)

        hook = self._lookup_hook(self._manager, self.name)
        assert hook is not None  # invalidation_reason() above ruled this out.
        call_kwargs = hook._apply_defaults(kwargs)
        hook._verify_all_args_are_provided(call_kwargs)
        return self._manager._hookexec(
            self.name, self.hookimpls, call_kwargs, self.firstresult
        )

    def __call__(self, **kwargs: object) -> object:
        """Execute the planned hook call; alias of :meth:`execute`."""
        return self.execute(**kwargs)

    def __repr__(self) -> str:
        return (
            f"<HookCallPlan {self.name!r}, "
            f"{len(self.hookimpls)} implementation(s)>"
        )
