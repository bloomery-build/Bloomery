"""bloomery charge {list,search,get,install}: browse ChargeRegistry
(github.com/bloomery-build/ChargeRegistry), download charges into
~/.bloomery/charges/<language>/, and install them via their alloy.
Charges are addressed as "<language>/<name>", one folder per language.
`install` also accepts ad-hoc "<language>:<package>" items (see
bloomery.charges).
"""

import os

import bloomery.charges as charges
from bloomery.color import entry_line, paint
from bloomery.errors import BloomeryError, ChargeNotFoundError, RegistryError
from bloomery.registry import RegistryClient

REGISTRY_REPO = "bloomery-build/ChargeRegistry"
LOCAL_CHARGE_DIR = os.path.join(os.path.expanduser("~"), ".bloomery", "charges")

_client = RegistryClient(REGISTRY_REPO)


def _find_entry(index, language, name):
    for entry in index.get("charges", []):
        if entry["name"].lower() == name.lower() and entry.get("language", "").lower() == language.lower():
            return entry
    return None


def list_command():
    index = _client.fetch_index()
    entries = index.get("charges", [])
    if not entries:
        print("No charges available.")
        return
    print("Available charges:")
    for entry in entries:
        ref = f"{entry.get('language', '?')}/{entry['name']}"
        print(entry_line(ref, entry.get("description", "")))


def search_command(query):
    index = _client.fetch_index()
    query = query.lower()
    hits = [
        e for e in index.get("charges", [])
        if query in e["name"].lower() or query in e.get("description", "").lower()
    ]
    if not hits:
        print(f"No charges match {query!r}.")
        return
    for entry in hits:
        ref = f"{entry.get('language', '?')}/{entry['name']}"
        print(entry_line(ref, entry.get("description", "")))


def get_command(ref, force=False):
    if "/" not in ref:
        raise BloomeryError(f"Charge ref must be '<language>/<name>' (e.g. 'python/requests'), got {ref!r}")
    language, name = ref.split("/", 1)

    index = _client.fetch_index()
    entry = _find_entry(index, language, name)
    if entry is None:
        available = ", ".join(f"{e.get('language', '?')}/{e['name']}" for e in index.get("charges", [])) or "(none)"
        raise RegistryError(f"Charge not found in ChargeRegistry: {ref}\n  Available: {available}")

    dest_dir = os.path.join(LOCAL_CHARGE_DIR, language.lower())
    dest = os.path.join(dest_dir, f"{name.lower()}.toml")
    if os.path.exists(dest) and not force:
        answer = input(f"{dest} already exists. Overwrite? [y/N] ").strip().lower()
        if answer != "y":
            print("Aborted.")
            return

    content = _client.fetch_file(entry["file"])
    os.makedirs(dest_dir, exist_ok=True)
    with open(dest, "wb") as f:
        f.write(content)
    print(paint(f"OK - wrote {dest}", "green"))


def install_command(items, verbose=False):
    if not items:
        raise BloomeryError(
            "Usage: bloomery charge install <language/name | language:package[@version]> ...")

    project_dir = os.getcwd()
    for item in items:
        if ":" in item:
            continue  # ad-hoc item, bypasses charge lookup entirely
        try:
            charges.load_charge(item, project_dir)
        except ChargeNotFoundError:
            get_command(item)

    lock = charges.install_charges(items, project_dir, verbose=verbose)
    print(paint(f"OK - installed {len(lock)} charge(s)", "green"))
    for ref, info in lock.items():
        extra = f" ({info['package']})" if "package" in info else ""
        version = f" {info['version']}" if info["version"] else ""
        print(f"  {paint(ref, 'cyan')}{extra}{paint(version, 'dim')} "
              f"(via {paint(info['alloy'], 'green')})")


def charge_group(argv):
    if not argv:
        raise BloomeryError(
            "Usage: bloomery charge <list|search|get|install> ...")

    sub, rest = argv[0], argv[1:]

    if sub == "list":
        list_command()
    elif sub == "search":
        if not rest:
            raise BloomeryError("Usage: bloomery charge search <query>")
        search_command(rest[0])
    elif sub == "get":
        refs = [a for a in rest if a != "--force"]
        if not refs:
            raise BloomeryError("Usage: bloomery charge get <language/name> [--force]")
        get_command(refs[0], force="--force" in rest)
    elif sub == "install":
        verbose = "--verbose" in rest
        items = [a for a in rest if a != "--verbose"]
        install_command(items, verbose=verbose)
    else:
        raise BloomeryError(f"Unknown charge subcommand: {sub!r}")
