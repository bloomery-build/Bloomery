class BloomeryError(Exception):
    """Base exception for Bloomery build errors."""


class CyclicDependencyError(BloomeryError):
    """Raised when a cycle is detected in the task dependency graph."""


class TaskFailedError(BloomeryError):
    """Raised when a task command exits with a non-zero code."""


class ConfigNotFoundError(BloomeryError):
    """Raised when a project or mold TOML file cannot be found."""


class ConfigParseError(BloomeryError):
    """Raised when a TOML file is malformed."""


class UnknownTargetError(BloomeryError):
    """Raised when a requested target matches no task."""


class MoldNotFoundError(ConfigNotFoundError):
    """Raised when a mold cannot be located on the search path."""


class RegistryError(BloomeryError):
    """Raised on a network/lookup failure talking to a GitHub-backed registry."""


class MoldDownloadError(RegistryError):
    """Raised when a mold cannot be fetched from MoldRegistry."""


class ChargeNotFoundError(RegistryError):
    """Raised when a charge cannot be located on the search path or registry."""


class AlloyNotFoundError(RegistryError):
    """Raised when an alloy cannot be located on the search path or registry."""


class ChargeBuildError(BloomeryError):
    """Raised when fetching or building a charge fails."""


class CyclicChargeDependencyError(BloomeryError):
    """Raised when a cycle is detected in the charge dependency graph."""
