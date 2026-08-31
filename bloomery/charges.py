"""Fetch/build instructions for one package, routed through an alloy.
Addressed as "<language>/<name>" at <root>/<language>/<name>.toml.
install_charges also takes ad-hoc "<language>:<package>[@version]" items
with no charge file needed.
"""

import json
import os
from collections import defaultdict, deque

from bloomery.alloys import load_alloy
from bloomery.config import parse_toml
from bloomery.context import Context
from bloomery.errors import ChargeBuildError, ChargeNotFoundError, CyclicChargeDependencyError
from bloomery.evaluator import Evaluator
from bloomery.runner import TaskRunner

VENDOR_DIRNAME = os.path.join(".bloomery", "vendor")

# default alloy per language, used when a charge/ad-hoc item doesn't set one explicitly
DEFAULT_ALLOY_BY_LANGUAGE = {
    "python": "pip",
    "node": "npm",
    "javascript": "npm",
    "rust": "cargo",
}

# per-alloy version pin syntax for ad-hoc "@version" items
_ADHOC_VERSION_FORMAT = {
    "pip": "=={}",
    "npm": "@{}",
    "cargo": "@{}",
}


def charge_search_path(project_dir, config=None):
    """Charge dirs in priority order: project-local wins, downloaded last"""
    yield os.path.join(project_dir, "charges")

    if config is not None:
        declared = config.get("meta", {}).get("charge_path", "")
        for part in declared.split(os.pathsep):
            if part.strip():
                yield os.path.join(project_dir, part.strip())

    for part in os.environ.get("BLOOMERY_CHARGE_PATH", "").split(os.pathsep):
        if part.strip():
            yield part.strip()

    yield os.path.join(os.path.expanduser("~"), ".bloomery", "charges")


def load_charge(ref, project_dir, config=None):
    """ref is "<language>/<name>", e.g. "python/requests"."""
    if "/" not in ref:
        raise ChargeNotFoundError(
            f"Charge ref must be '<language>/<name>' (e.g. 'python/requests'), got {ref!r}")

    searched = []
    for directory in charge_search_path(project_dir, config):
        candidate = os.path.join(directory, f"{ref.lower()}.toml")
        searched.append(candidate)
        if os.path.exists(candidate):
            return parse_toml(candidate)

    raise ChargeNotFoundError(
        "Charge not found: {}\n  Searched:\n{}".format(
            ref, "\n".join(f"    {p}" for p in searched))
    )


def resolve_dependency_order(charges):
    """charges: {ref: charge_dict}. Kahn's algorithm, deps before dependents."""
    indegree = {ref: 0 for ref in charges}
    graph = defaultdict(list)
    for ref, charge in charges.items():
        for dep in charge.get("charge", {}).get("depends", []):
            if dep not in charges:
                raise ChargeNotFoundError(
                    f"Charge '{ref}' depends on '{dep}', which wasn't loaded")
            graph[dep].append(ref)
            indegree[ref] += 1

    queue = deque(r for r in charges if indegree[r] == 0)
    order = []
    while queue:
        cur = queue.popleft()
        order.append(cur)
        for nxt in graph[cur]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)

    if len(order) != len(charges):
        raise CyclicChargeDependencyError("Cycle detected in charge dependencies")
    return order


def _resolve_alloy_name(meta):
    language = meta.get("language", "")
    return meta.get("alloy") or DEFAULT_ALLOY_BY_LANGUAGE.get(language, language)


def _flatten(prefix, table, out):
    """{'pip': {'package': 'x'}} -> {'pip.package': 'x'}."""
    for key, value in table.items():
        full_key = f"{prefix}.{key}"
        if isinstance(value, dict):
            _flatten(full_key, value, out)
        else:
            out[full_key] = value


def _charge_evaluator(charge, project_dir, verbose=False):
    """[charge] fields flatten to charge.*, every other table to
    charge.<table>.*. charge.package defaults to charge.name."""
    variables = {}
    for section, table in charge.items():
        if not isinstance(table, dict):
            continue
        prefix = "charge" if section == "charge" else f"charge.{section}"
        _flatten(prefix, table, variables)
    variables.setdefault("charge.package", variables.get("charge.name", ""))
    ctx = Context(project_dir=project_dir, variables=variables, verbose=verbose)
    return Evaluator(ctx)


def install_charge(charge, alloy, project_dir, vendor_dir, verbose=False):
    """Fetch/build one charge. Returns the vendor path it landed in."""
    meta = charge.get("charge", {})
    name = meta.get("name", "unknown")
    dest = os.path.join(vendor_dir, name)
    evaluator = _charge_evaluator(charge, project_dir, verbose=verbose)

    if alloy.get("alloy", {}).get("name") == "source":
        _install_from_source(meta, charge, evaluator, dest)
    else:
        _install_via_alloy(meta, alloy, evaluator, project_dir, dest)

    return dest


def _install_via_alloy(meta, alloy, evaluator, project_dir, dest):
    commands = alloy.get("commands", {})
    if "install" not in commands:
        raise ChargeBuildError(
            f"Alloy {alloy.get('alloy', {}).get('name')!r} has no 'install' command")

    command_line = evaluator.resolve_str(commands["install"])
    result = TaskRunner._execute(command_line, project_dir)
    if result.returncode != 0:
        raise ChargeBuildError(
            f"Charge '{meta.get('name')}' failed to install (exit {result.returncode})")

    os.makedirs(dest, exist_ok=True)  # marker only; the manager owns the actual install


def _install_from_source(meta, charge, evaluator, dest):
    source = charge.get("source", {})
    kind = source.get("kind", "git")
    os.makedirs(dest, exist_ok=True)

    if kind == "git":
        url = evaluator.resolve_str(source.get("url", ""))
        ref = evaluator.resolve_str(source.get("ref", ""))
        clone = f'git clone --branch "{ref}" --depth 1 "{url}" .' if ref \
            else f'git clone --depth 1 "{url}" .'
        result = TaskRunner._execute(clone, dest)
    elif kind in ("tarball", "zip"):
        url = evaluator.resolve_str(source.get("url", ""))
        single_file = source.get("single_file")
        if single_file:
            result = TaskRunner._execute(f'curl -L "{url}" -o "{single_file}"', dest)
        else:
            archive = "src.tar.gz" if kind == "tarball" else "src.zip"
            extract = f'tar xzf "{archive}"' if kind == "tarball" else f'tar xf "{archive}"'
            result = TaskRunner._execute(
                f'curl -L "{url}" -o "{archive}" && {extract}', dest)
    else:
        raise ChargeBuildError(f"Unknown source kind: {kind!r}")

    if result.returncode != 0:
        raise ChargeBuildError(
            f"Charge '{meta.get('name')}' fetch failed (exit {result.returncode})")

    build = charge.get("build", {})
    if not build:
        return

    steps = []
    if "configure" in build:
        steps.append(evaluator.resolve_str(build["configure"]))
    if "command" in build:
        cmd = evaluator.resolve_str(build["command"])
        flags = evaluator.resolve_str(build.get("flags", ""))
        steps.append(f"{cmd} {flags}".strip())

    for step in steps:
        result = TaskRunner._execute(step, dest)
        if result.returncode != 0:
            raise ChargeBuildError(
                f"Charge '{meta.get('name')}' build step failed: {step!r} "
                f"(exit {result.returncode})")


def _parse_item(item):
    """"python/requests" -> a ref. "python:flask[@version]" -> ad-hoc."""
    if ":" in item:
        language, spec = item.split(":", 1)
        package, _, version = spec.partition("@")
        return ("adhoc", language, package, version)
    return ("ref", item)


def _synthetic_charge(language, package, version, alloy_name):
    version_suffix = ""
    if version:
        fmt = _ADHOC_VERSION_FORMAT.get(alloy_name, "@{}")
        version_suffix = fmt.format(version)
    return {"charge": {"name": package, "language": language, "alloy": alloy_name,
                        "package": package, "version": version_suffix}}


def install_charges(items, project_dir, config=None, verbose=False):
    """Install a mix of refs and ad-hoc items. Returns the lock dict,
    also written to .bloomery/vendor/lock.json."""
    charges_by_ref = {}
    adhoc_items = []
    for item in items:
        kind, *rest = _parse_item(item)
        if kind == "adhoc":
            adhoc_items.append((item, *rest))
        else:
            ref = rest[0]
            charges_by_ref[ref] = load_charge(ref, project_dir, config)

    order = resolve_dependency_order(charges_by_ref) if charges_by_ref else []

    vendor_dir = os.path.join(project_dir, VENDOR_DIRNAME)
    os.makedirs(vendor_dir, exist_ok=True)

    lock = {}
    for ref in order:
        charge = charges_by_ref[ref]
        meta = charge.get("charge", {})
        alloy_name = _resolve_alloy_name(meta)
        alloy = load_alloy(alloy_name, project_dir, config)
        install_charge(charge, alloy, project_dir, vendor_dir, verbose=verbose)
        lock[ref] = {"version": meta.get("version", ""), "alloy": alloy_name}

    for item, language, package, version in adhoc_items:
        alloy_name = _resolve_alloy_name({"language": language})
        alloy = load_alloy(alloy_name, project_dir, config)
        charge = _synthetic_charge(language, package, version, alloy_name)
        install_charge(charge, alloy, project_dir, vendor_dir, verbose=verbose)
        lock[item] = {"version": version, "alloy": alloy_name, "package": package}

    with open(os.path.join(vendor_dir, "lock.json"), "w", encoding="utf-8") as f:
        json.dump(lock, f, indent=2)
    return lock


def ensure_installed(config, project_dir):
    """No-op unless [charges] auto_install is set in the manifest."""
    charges_cfg = config.get("charges", {})
    if not charges_cfg.get("auto_install", False):
        return
    install_charges(charges_cfg.get("use", []), project_dir, config)
