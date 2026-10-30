__all__ = [
    "HookCallError",
    "HookCaller",
    "HookImpl",
    "HookRelay",
    "HookimplMarker",
    "HookimplOpts",
    "HookspecMarker",
    "HookspecOpts",
    "PluggyTeardownRaisedWarning",
    "PluggyWarning",
    "PluginManager",
    "PluginManagerSnapshot",
    "PluginValidationError",
    "Result",
    "SnapshotCrossManagerError",
    "SnapshotDestroyedError",
    "SnapshotError",
    "SnapshotExpiredError",
    "SnapshotReusedError",
    "__version__",
]
from ._hooks import HookCaller
from ._hooks import HookImpl
from ._hooks import HookimplMarker
from ._hooks import HookimplOpts
from ._hooks import HookRelay
from ._hooks import HookspecMarker
from ._hooks import HookspecOpts
from ._manager import PluginManager
from ._manager import PluginValidationError
from ._result import HookCallError
from ._result import Result
from ._snapshot import PluginManagerSnapshot
from ._snapshot import SnapshotCrossManagerError
from ._snapshot import SnapshotDestroyedError
from ._snapshot import SnapshotError
from ._snapshot import SnapshotExpiredError
from ._snapshot import SnapshotReusedError
from ._warnings import PluggyTeardownRaisedWarning
from ._warnings import PluggyWarning


def __getattr__(name: str) -> str:
    if name == "__version__":
        from importlib.metadata import version

        return version("pluggy")

    raise AttributeError(f"module {__name__} has no attribute {name!r}")
