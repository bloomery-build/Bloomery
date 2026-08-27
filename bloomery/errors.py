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
