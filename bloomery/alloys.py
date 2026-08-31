import os

from bloomery.config import parse_toml
from bloomery.errors import AlloyNotFoundError


def alloy_search_path(project_dir, config=None):
    """Alloy dirs in priority order: project-local wins, bundled last"""
    yield os.path.join(project_dir, "alloys")

    if config is not None:
        declared = config.get("meta", {}).get("alloy_path", "")
        for part in declared.split(os.pathsep):
            if part.strip():
                yield os.path.join(project_dir, part.strip())

    for part in os.environ.get("BLOOMERY_ALLOY_PATH", "").split(os.pathsep):
        if part.strip():
            yield part.strip()

    yield os.path.join(os.path.expanduser("~"), ".bloomery", "alloys")
    yield os.path.join(os.path.dirname(os.path.abspath(__file__)), "alloys")


def load_alloy(name, project_dir, config=None):
    """Load an alloy by name (usually a language name, e.g. 'python' -> pip)"""
    if not name:
        return None

    searched = []
    for directory in alloy_search_path(project_dir, config):
        candidate = os.path.join(directory, f"{name.lower()}.toml")
        searched.append(candidate)
        if os.path.exists(candidate):
            return parse_toml(candidate)

    raise AlloyNotFoundError(
        "Alloy not found: {}\n  Searched:\n{}".format(
            name, "\n".join(f"    {p}" for p in searched))
    )
