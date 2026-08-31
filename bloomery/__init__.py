"""
Interpolation (in any string):
    {name}              Variable / dispatch-table reference
    {env.NAME}          Environment variable
    {mold.Field}        Mold definition lookup
    {task.T.Field}      Another task's field
    {task.T.outputs}    Files another task produced

Reserved-key tables:
    { on = "var", <value> = ..., default = ... }   Dispatch on a variable's
                                                     current value
    { ending = [".ext", ...] }                      Files by extension
    { in = "dir", ending = [".ext", ...] }           ...scoped to a directory
    { matching = "glob" }                            Files by glob pattern
    { shell = "cmd" }                                Captured stdout of cmd
    { exists = "path" }                              "true" / "false"
    { prefix = "-D", items = [...] }                 Prefix each item, joined

A dispatch value's own "true"/"false"/"windows"/... branches, and every
reserved-key argument, are themselves resolved recursively, a branch can
be another dispatch table, a list, or a plain string.

Task fields:
    mode = "per-file"   One command per input file, cached individually
    headers = [...]     Hashed but kept off the command line (#include deps)
    depfile = "..."     Make-format depfile to read #include deps from
    outputs = [...]     Paths that Bloomery should check for existence to see if the task needs rerun
    depends = ["..."]   Task names this one depends on
    always_run = true   Skip the cache, always execute
    default = false     Excluded from the default (no-target) build

Usage:
    bloomery [targets...] [options]
    python -m bloomery [targets...] [options]

Options:
    --clean          Force full rebuild (ignore cache)
    --dry-run        Show commands without executing
    --list           List available targets and exit
    --verbose        Show resolution details
    -D VAR=VALUE     Define/override a variable
    --profile NAME   Activate a profile (overlays [profiles.NAME])
    -j, --jobs N     Run independent tasks in parallel (0 = one per CPU)
    --keep-going     Don't cancel sibling tasks after a failure
    --manifest       Path to the project manifest
    --version        Print version and exit

Self-management:
    bloomery install     pip install -e the git checkout (dev mode)
    bloomery update      git pull a dev checkout, else pip upgrade
    bloomery uninstall   pip uninstall bloomery-build
    bloomery init        scaffold a bloomery.toml in the current directory
"""

from bloomery._version import __version__
from bloomery.cache import BuildCache, parse_depfile
from bloomery.cli import main, run_tasks
from bloomery.config import (
    list_targets,
    load_mold,
    load_profiles,
    load_variables,
    mold_search_path,
    parse_toml,
)
from bloomery.context import Context
from bloomery.dag import TaskDAG
from bloomery.errors import (
    BloomeryError,
    ConfigNotFoundError,
    ConfigParseError,
    CyclicDependencyError,
    MoldNotFoundError,
    TaskFailedError,
    UnknownTargetError,
)
from bloomery.evaluator import Evaluator
from bloomery.plugins import PluginManager
from bloomery.runner import TaskRunner
from bloomery.selfmanage import cli

__all__ = [
    "BloomeryError",
    "BuildCache",
    "ConfigNotFoundError",
    "ConfigParseError",
    "Context",
    "CyclicDependencyError",
    "Evaluator",
    "MoldNotFoundError",
    "PluginManager",
    "TaskDAG",
    "TaskFailedError",
    "TaskRunner",
    "UnknownTargetError",
    "__version__",
    "cli",
    "list_targets",
    "load_mold",
    "load_profiles",
    "load_variables",
    "main",
    "mold_search_path",
    "parse_depfile",
    "parse_toml",
    "run_tasks",
]
