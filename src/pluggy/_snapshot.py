"""
Immutable :class:`PluginManagerSnapshot` and isolated clone restoration.

A snapshot captures a point-in-time, immutable view of a
:class:`~pluggy.PluginManager`: its hook specifications, the registered
plugins and their names, blocked names, hook implementation metadata and
ordering, the manager's structural version, and its :ref:`historic
<historic>` call history.

Restoring a snapshot builds a brand new manager which shares the immutable
hook specifications and hook implementation metadata but owns independent
registries, hook caller lists and historic histories. Building a clone never
runs plugin code: implementations registered at snapshot time are wired up
directly and are treated as already caught up with inherited historic events,
while events are replayed (each at most once) to plugins registered in the
clone afterwards.
"""

from __future__ import annotations

from collections.abc import Mapping
import types
from typing import Final
from typing import TypeAlias
import uuid

from ._hooks import _Plugin
from ._hooks import HookCaller
from ._hooks import HookImpl
from ._hooks import HookSpec
from ._manager import _HistoricReplay
from ._manager import PluginManager


#: Snapshot record format understood by this pluggy version.
_FORMAT_VERSION: Final[int] = 1
#: Record formats that can still be restored.
_SUPPORTED_FORMAT_VERSIONS: Final[frozenset[int]] = frozenset({_FORMAT_VERSION})

_STATE_ACTIVE: Final = 0
_STATE_CONSUMED: Final = 1
_STATE_DESTROYED: Final = 2

#: ``(plugin name, plugin or the ``None`` blocked-name sentinel)`` pairs in
#: registration order.
_RegistryRecord: TypeAlias = tuple[tuple[str, _Plugin | None], ...]


class SnapshotError(Exception):
    """Base class for failures involving a :class:`PluginManagerSnapshot`."""


class SnapshotExpiredError(SnapshotError):
    """A snapshot uses a record format no longer supported.

    Ordinary source-manager changes never expire a snapshot -- it always
    restores to the exact state it was taken at. Expiration only occurs when
    the on-record format version is older or newer than what this pluggy
    version can restore.
    """


class SnapshotCrossManagerError(SnapshotError):
    """A snapshot is restored into a manager it does not belong to.

    This covers mismatched ``project_name`` values as well as attempts to
    restore a snapshot back into the very manager that created it.
    """


class SnapshotReusedError(SnapshotError):
    """An already-consumed snapshot is restored a second time.

    Each snapshot can produce a single clone. Take another snapshot to create
    further independent contexts.
    """


class SnapshotDestroyedError(SnapshotError):
    """A destroyed snapshot is used after :meth:`PluginManagerSnapshot.destroy`."""


class _HookState:
    """Frozen per-hook record captured from a :class:`HookCaller`."""

    __slots__ = ("history", "impls", "name", "spec")

    def __init__(
        self,
        name: str,
        spec: HookSpec | None,
        impls: tuple[HookImpl, ...],
        history: tuple[Mapping[str, object], ...] | None,
    ) -> None:
        self.name = name
        # HookSpec objects are immutable after creation and may be shared.
        self.spec = spec
        # Ordered hook implementations. HookImpl objects are immutable and
        # are shared; only the containing list is cloned per manager.
        self.impls = impls
        # ``None`` for non-historic hooks; otherwise the frozen historic event
        # kwargs in call order. Source result callbacks are intentionally not
        # carried over, so replay in a clone cannot leak into source code.
        self.history = history


class PluginManagerSnapshot:
    """An immutable point-in-time record of a :class:`~pluggy.PluginManager`.

    Create one with :meth:`PluginManager.snapshot` and build an isolated
    clone with :meth:`restore`. The captured data cannot be mutated; later
    changes to the source manager never affect an existing snapshot.

    A snapshot is single-use: :meth:`restore` (or
    :meth:`PluginManager.restore_snapshot`) consumes it, and restoring it
    again raises :class:`SnapshotReusedError`. Call :meth:`destroy` to
    release it deliberately.
    """

    __slots__ = (
        "_format_version",
        "_hooks",
        "_manager_version",
        "_origin_uuid",
        "_project_name",
        "_registry",
        "_state",
    )

    def __init__(
        self,
        *,
        project_name: str,
        origin_uuid: uuid.UUID,
        manager_version: int,
        format_version: int,
        registry: _RegistryRecord,
        hooks: tuple[_HookState, ...],
    ) -> None:
        """:meta private:"""
        self._project_name: Final = project_name
        self._origin_uuid: Final = origin_uuid
        self._manager_version: Final = manager_version
        self._format_version: Final = format_version
        self._registry: Final = registry
        self._hooks: Final = hooks
        self._state: int = _STATE_ACTIVE

    @property
    def project_name(self) -> str:
        """The ``project_name`` of the originating manager."""
        return self._project_name

    @property
    def origin_uuid(self) -> uuid.UUID:
        """The unique identity of the originating manager."""
        return self._origin_uuid

    @property
    def manager_version(self) -> int:
        """Structural :attr:`~pluggy.PluginManager.manager_version` captured."""
        return self._manager_version

    @property
    def format_version(self) -> int:
        """Snapshot record format version."""
        return self._format_version

    @property
    def plugin_names(self) -> tuple[str, ...]:
        """Names of the registered plugins, in registration order."""
        return tuple(name for name, plugin in self._registry if plugin is not None)

    @property
    def blocked_names(self) -> frozenset[str]:
        """Names which were blocked when the snapshot was taken."""
        return frozenset(name for name, plugin in self._registry if plugin is None)

    @property
    def plugins(self) -> tuple[_Plugin, ...]:
        """The registered plugin objects, in registration order."""
        return tuple(plugin for _, plugin in self._registry if plugin is not None)

    @property
    def hook_names(self) -> tuple[str, ...]:
        """Names of the hook callers present in the snapshot."""
        return tuple(state.name for state in self._hooks)

    @property
    def is_consumed(self) -> bool:
        """Whether the snapshot has already produced a clone."""
        return self._state == _STATE_CONSUMED

    @property
    def is_destroyed(self) -> bool:
        """Whether :meth:`destroy` has been called."""
        return self._state == _STATE_DESTROYED

    @property
    def is_expired(self) -> bool:
        """Whether the record format is no longer supported."""
        return self._format_version not in _SUPPORTED_FORMAT_VERSIONS

    def restore(self) -> PluginManager:
        """Build and return a new, isolated :class:`~pluggy.PluginManager`.

        The clone contains the snapshot's hookspecs, plugins, blocked names
        and hook implementation ordering, and its historic events replay to
        plugins registered in the clone from now on (each event at most once
        per plugin). No plugin code runs while the clone is built and the
        source manager is never modified.

        Raises a :class:`SnapshotError` subclass if the snapshot is no longer
        usable; a failed restore does not consume the snapshot.
        """
        clone = PluginManager(self._project_name)
        restore_snapshot(clone, self)
        return clone

    def destroy(self) -> None:
        """Invalidate the snapshot. Any later restore raises
        :class:`SnapshotDestroyedError`."""
        self._state = _STATE_DESTROYED

    def __repr__(self) -> str:
        state = ("active", "consumed", "destroyed")[self._state]
        return (
            f"<PluginManagerSnapshot project={self._project_name!r}"
            f" manager_version={self._manager_version} {state}>"
        )


def take_snapshot(manager: PluginManager) -> PluginManagerSnapshot:
    """Capture an immutable snapshot of ``manager`` without running plugins."""
    hooks: list[_HookState] = []
    for hook_name in manager.hook.__dict__:
        hook: HookCaller = getattr(manager.hook, hook_name)
        history: tuple[Mapping[str, object], ...] | None = None
        if hook.is_historic():
            assert hook._call_history is not None
            # Freeze the kwargs; source result callbacks stay behind so a
            # clone can never invoke source-scoped code through replay.
            history = tuple(
                types.MappingProxyType(dict(kwargs))
                for kwargs, _result_callback in hook._call_history
            )
        hooks.append(_HookState(hook.name, hook.spec, tuple(hook._hookimpls), history))
    return PluginManagerSnapshot(
        project_name=manager.project_name,
        origin_uuid=manager._manager_uuid,
        manager_version=manager._pm_version,
        format_version=_FORMAT_VERSION,
        registry=tuple(manager._name2plugin.items()),
        hooks=tuple(hooks),
    )


def restore_snapshot(target: PluginManager, snapshot: PluginManagerSnapshot) -> None:
    """Populate a fresh ``target`` manager from ``snapshot``, atomically.

    All validation happens before any target state is committed and no
    plugin code executes, so every failure leaves both the target and the
    snapshot usable.
    """
    # Lifecycle and identity guards -- before any plugin code can run.
    if snapshot._state == _STATE_DESTROYED:
        raise SnapshotDestroyedError(
            f"snapshot of project {snapshot._project_name!r} has been destroyed"
        )
    if snapshot._state == _STATE_CONSUMED:
        raise SnapshotReusedError(
            f"snapshot of project {snapshot._project_name!r} has already been "
            "restored; take another snapshot for another clone"
        )
    if snapshot.is_expired:
        raise SnapshotExpiredError(
            f"snapshot format version {snapshot._format_version} is not supported "
            f"by this pluggy version (supported: {sorted(_SUPPORTED_FORMAT_VERSIONS)})"
        )
    if target.project_name != snapshot._project_name:
        raise SnapshotCrossManagerError(
            f"snapshot belongs to project {snapshot._project_name!r}, cannot "
            f"restore it into manager of project {target.project_name!r}"
        )
    if target._manager_uuid == snapshot._origin_uuid:
        raise SnapshotCrossManagerError(
            "cannot restore a snapshot into the manager that created it"
        )
    if target._name2plugin or target.hook.__dict__:
        raise SnapshotError(
            "a snapshot can only be restored into an empty PluginManager "
            "(no plugins, blocked names or hookspecs registered)"
        )

    # Defensive consistency checks on the immutable record. A manager cannot
    # produce an inconsistent snapshot, but a tampered record must fail
    # without leaving a half-built manager.
    seen_identities: set[int] = set()
    for name, plugin in snapshot._registry:
        if plugin is None:
            continue
        if id(plugin) in seen_identities:
            raise SnapshotError(
                f"snapshot registers the same plugin under multiple names: {plugin!r}"
            )
        seen_identities.add(id(plugin))

    # Build all hook callers locally first; nothing is committed yet.
    built: list[tuple[_HookState, HookCaller]] = []
    for state in snapshot._hooks:
        caller = HookCaller(state.name, target._hookexec)
        if state.spec is not None:
            caller.spec = state.spec
        if state.history is not None:
            caller._call_history = [(dict(kwargs), None) for kwargs in state.history]
        caller._hookimpls.extend(state.impls)
        built.append((state, caller))

    # Commit the captured state in place, preserving the manager's identity
    # of its registry and relay containers.
    for name, plugin in snapshot._registry:
        target._name2plugin[name] = plugin
    for state, caller in built:
        setattr(target.hook, state.name, caller)

    # Enable at-most-once historic replay and seed it: every plugin present
    # at fork time is considered to have already received every inherited
    # event, so unregistering and re-registering one never replays them.
    replay = _HistoricReplay()
    for state, _caller in built:
        if state.history is None:
            continue
        for impl in state.impls:
            replay.delivered(impl.plugin).update(
                (state.name, index) for index in range(len(state.history))
            )
    target._historic_replay = replay
    target._pm_version = snapshot._manager_version

    snapshot._state = _STATE_CONSUMED
