import os

try:
    import tomllib
except ModuleNotFoundError:                # Python < 3.11
    import tomli as tomllib

from bloomery.errors import ConfigNotFoundError, ConfigParseError, MoldNotFoundError


def parse_toml(filepath):
    """Parse a project/mold file into a plain nested dict"""
    if not os.path.exists(filepath):
        raise ConfigNotFoundError(f"Missing file: {filepath}")
    try:
        with open(filepath, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigParseError(f"Could not parse {filepath}:\n  {e}") from e


def load_variables(config):
    """Raw (unresolved) [variables] table"""
    return dict(config.get("variables", {}))


def load_profiles(config):
    """Raw (unresolved) [profiles.*] tables"""
    return {name: dict(vals) for name, vals in config.get("profiles", {}).items()}


def mold_search_path(project_dir, config=None):
    """Mold dirs in priority order: local wins, bundled last"""
    yield project_dir

    if config is not None:
        declared = config.get("meta", {}).get("mold_path", "")
        for part in declared.split(os.pathsep):
            if part.strip():
                yield os.path.join(project_dir, part.strip())

    for part in os.environ.get("BLOOMERY_MOLD_PATH", "").split(os.pathsep):
        if part.strip():
            yield part.strip()

    yield os.path.join(os.path.expanduser("~"), ".bloomery", "molds")
    yield os.path.join(os.path.dirname(os.path.abspath(__file__)), "molds")


def load_mold(system_name, project_dir, config=None, _seen=None):
    """Load a mold, following Extends. Missing is an error, not a warning"""
    if not system_name:
        return None

    searched = []
    for directory in mold_search_path(project_dir, config):
        candidate = os.path.join(directory, f"{system_name.lower()}.toml")
        searched.append(candidate)
        if os.path.exists(candidate):
            mold = parse_toml(candidate)
            return _apply_mold_inheritance(
                mold, system_name, project_dir, config, _seen)

    raise MoldNotFoundError(
        "Mold not found: {}\n  Searched:\n{}".format(
            system_name, "\n".join(f"    {p}" for p in searched))
    )


def _apply_mold_inheritance(mold, name, project_dir, config, seen):
    """Merge parent [definitions] under the child's"""
    parent_name = mold.get("bloomery", {}).get("extends", "").strip()
    if not parent_name:
        return mold

    seen = seen or []
    if name.lower() in [s.lower() for s in seen]:
        raise MoldNotFoundError(
            f"Cyclic mold inheritance: {' -> '.join(seen + [name])}")

    parent = load_mold(parent_name, project_dir, config, _seen=seen + [name])
    if parent is None or "definitions" not in parent:
        return mold

    defs = mold.setdefault("definitions", {})
    for key, value in parent["definitions"].items():
        defs.setdefault(key, value)
    return mold


def list_targets(config):
    """Print available targets from [tasks]."""
    tasks = config.get("tasks", {})
    if not tasks:
        print("No tasks defined.")
        return

    print("Available targets:")
    for name, task in tasks.items():
        deps = task.get("depends", [])
        dep_str = f"  (depends: {', '.join(deps)})" if deps else ""
        print(f"  {name}{dep_str}")
