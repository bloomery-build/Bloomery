import os

from bloomery.color import entry_line, paint
from bloomery.errors import BloomeryError, RegistryError
from bloomery.registry import RegistryClient
from bloomery.alloys import find_alloy

REGISTRY_REPO = "bloomery-build/AlloyRegistry"
LOCAL_ALLOY_DIR = os.path.join(os.path.expanduser("~"), ".bloomery", "alloys")

_client = RegistryClient(REGISTRY_REPO)


def _find_entry(index, name):
    for entry in index.get("alloys", []):
        if entry["name"].lower() == name.lower():
            return entry
    return None


def list_command():
    index = _client.fetch_index()
    entries = index.get("alloys", [])
    if not entries:
        print("No alloys available.")
        return
    print("Available alloys:")
    for entry in entries:
        language = f"({entry.get('language', '?')})"
        description = f"{language} {entry['description']}" if entry.get("description") else language
        if find_alloy(entry["name"], ".", index) is not None:
            print(entry_line(entry["name"], description, 'green'))
        else:
            print(entry_line(entry["name"], description))
        


def get_command(name, force=False):
    index = _client.fetch_index()
    entry = _find_entry(index, name)
    if entry is None:
        available = ", ".join(e["name"] for e in index.get("alloys", [])) or "(none)"
        raise RegistryError(f"Alloy not found in AlloyRegistry: {name}\n  Available: {available}")

    dest = os.path.join(LOCAL_ALLOY_DIR, f"{name.lower()}.toml")
    if os.path.exists(dest) and not force:
        answer = input(f"{dest} already exists. Overwrite? [y/N] ").strip().lower()
        if answer != "y":
            print("Aborted.")
            return

    content = _client.fetch_file(entry["file"])
    os.makedirs(LOCAL_ALLOY_DIR, exist_ok=True)
    with open(dest, "wb") as f:
        f.write(content)
    print(paint(f"OK - wrote {dest}", "green"))


def alloy_group(argv):
    if not argv:
        raise BloomeryError("Usage: bloomery alloy <list|get> ...")

    sub, rest = argv[0], argv[1:]

    if sub == "list":
        list_command()
    elif sub == "get":
        names = [a for a in rest if a != "--force"]
        if not names:
            raise BloomeryError("Usage: bloomery alloy get <name> [--force]")
        get_command(names[0], force="--force" in rest)
    else:
        raise BloomeryError(f"Unknown alloy subcommand: {sub!r}")
