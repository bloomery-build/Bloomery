"""bloomery mold {list,search,get,init} to browse and download molds from
MoldRegistry (github.com/bloomery-build/MoldRegistry) into ~/.bloomery/molds,
where mold_search_path already looks for them.
"""

import os

from bloomery.color import entry_line, paint
from bloomery.errors import BloomeryError, MoldDownloadError
from bloomery.registry import RegistryClient
from bloomery.config import find_mold

REGISTRY_REPO = "bloomery-build/MoldRegistry"
LOCAL_MOLD_DIR = os.path.join(os.path.expanduser("~"), ".bloomery", "molds")
TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "storage")

_client = RegistryClient(REGISTRY_REPO)


def _find_entry(index, name):
    for entry in index.get("molds", []):
        if entry["name"].lower() == name.lower():
            return entry
    return None


def list_command():
    index = _client.fetch_index()
    molds = index.get("molds", [])
    if not molds:
        print("No molds available.")
        return
    print("Available molds:")
    for entry in molds:
        if find_mold(entry["name"], ".", index) is not None:
            print(entry_line(entry["name"], entry.get("description", ""), 'green'))
        else:
            print(entry_line(entry["name"], entry.get("description", "")))


def search_command(query):
    index = _client.fetch_index()
    query = query.lower()
    hits = [
        e for e in index.get("molds", [])
        if query in e["name"].lower() or query in e.get("description", "").lower()
    ]
    if not hits:
        print(f"No molds match {query!r}.")
        return
    for entry in hits:
        
        print(entry_line(entry["name"], entry.get("description", "")))


def get_command(name, force=False):
    index = _client.fetch_index()
    entry = _find_entry(index, name)
    if entry is None:
        available = ", ".join(e["name"] for e in index.get("molds", [])) or "(none)"
        raise MoldDownloadError(f"Mold not found in MoldRegistry: {name}\n  Available: {available}")

    dest = os.path.join(LOCAL_MOLD_DIR, f"{name.lower()}.toml")
    if os.path.exists(dest) and not force:
        answer = input(f"{dest} already exists. Overwrite? [y/N] ").strip().lower()
        if answer != "y":
            print("Aborted.")
            return

    content = _client.fetch_file(entry["file"])
    os.makedirs(LOCAL_MOLD_DIR, exist_ok=True)
    with open(dest, "wb") as f:
        f.write(content)
    print(paint(f"OK - wrote {dest}", "green"))


def init_command(name):
    template_path = os.path.join(TEMPLATE_DIR, "mold_init.toml")
    if not os.path.exists(template_path):
        raise BloomeryError(f"Missing mold template: {template_path}")

    with open(template_path, "r", encoding="utf-8") as f:
        content = f.read()

    content = content.replace("{{name}}", name.lower())
    content = content.replace("{{Name}}", name)

    dest = f"./{name.lower()}.toml"
    with open(dest, "w", encoding="utf-8") as f:
        f.write(content)
    print(paint(f"OK - wrote {dest}", "green"))
    print(f"Test it locally with: BLOOMERY_MOLD_PATH=. bloomery --manifest <project> ...")
    print("When it's ready, contribute it via a PR to bloomery-build/MoldRegistry"
          " (add the file and an index.json entry).")


def mold_group(argv):
    if not argv:
        raise BloomeryError(
            "Usage: bloomery mold <list|search|get|init> ...")

    sub, rest = argv[0], argv[1:]

    if sub == "list":
        list_command()
    elif sub == "search":
        if not rest:
            raise BloomeryError("Usage: bloomery mold search <query>")
        search_command(rest[0])
    elif sub == "get":
        names = [a for a in rest if a != "--force"]
        if not names:
            raise BloomeryError("Usage: bloomery mold get <name> [--force]")
        get_command(names[0], force="--force" in rest)
    elif sub == "init":
        if not rest:
            raise BloomeryError("Usage: bloomery mold init <name>")
        init_command(rest[0])
    else:
        raise BloomeryError(f"Unknown mold subcommand: {sub!r}")
