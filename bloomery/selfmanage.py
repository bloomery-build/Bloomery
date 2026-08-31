import os
import subprocess
import sys

from bloomery.alloyregistry import alloy_group
from bloomery.chargeregistry import charge_group
from bloomery.cli import main
from bloomery.errors import BloomeryError
from bloomery.moldregistry import mold_group

REPO_URL = "https://github.com/hydrophobis/Bloomery"
DEV_DIR = os.path.join(os.path.expanduser("~"), ".bloomery", "src")

# bundled with the package, same fallback as bloomery/molds/
TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "storage")


def repo_root():
    """The git checkout backing this install, or None"""
    for candidate in (os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      DEV_DIR):
        if os.path.isdir(os.path.join(candidate, ".git")):
            return candidate
    return None


def _run(*cmd):
    print("+", " ".join(cmd))
    if subprocess.run(list(cmd)).returncode != 0:
        raise BloomeryError(f"command failed: {' '.join(cmd)}")


def install_command():
    """pip install -e the checkout, cloning one if needed"""
    root = repo_root()
    if root is None:
        os.makedirs(os.path.dirname(DEV_DIR), exist_ok=True)
        _run("git", "clone", REPO_URL, DEV_DIR)
        root = DEV_DIR
    _run(sys.executable, "-m", "pip", "install", "-e", root)
    print(f"OK - 'bloomery' installed in dev mode from {root}")


def update_command():
    """Git pull for a dev checkout, else upgrade the installed package"""
    root = repo_root()
    if root is not None:
        _run("git", "-C", root, "pull", "--ff-only")
        print(f"OK - updated {root}")
        return
    _run(sys.executable, "-m", "pip", "install", "--upgrade", "bloomery-build")
    print("OK - upgraded bloomery-build from PyPI")


def uninstall_command():
    _run(sys.executable, "-m", "pip", "uninstall", "-y", "bloomery-build")


def init_command():
    """Scaffold a project.toml in the current directory from the bundled template"""
    template_path = os.path.join(TEMPLATE_DIR, "init.toml")
    if not os.path.exists(template_path):
        raise BloomeryError(f"Missing init template: {template_path}")

    with open(template_path, "r", encoding="utf-8") as f:
        content = f.read()

    project_name = input("Enter the name of the project: ")
    project_system = input("Enter the system/mold name (optional): ")

    content = content.replace("{{name}}", project_name)
    content = content.replace(
        "{{system}}",
        f'system = "{project_system}"' if project_system else "")

    with open("./bloomery.toml", "w", encoding="utf-8") as f:
        f.write(content)
    print("OK - wrote bloomery.toml")


SELF_COMMANDS = {
    "install": install_command,
    "update": update_command,
    "uninstall": uninstall_command,
    "init": init_command,
}

SELF_GROUPS = {
    "mold": mold_group,
    "charge": charge_group,
    "alloy": alloy_group,
}


def cli():
    try:
        # before argparse, which would read these as a project path
        if len(sys.argv) == 2 and sys.argv[1] in SELF_COMMANDS:
            SELF_COMMANDS[sys.argv[1]]()
            return 0
        if len(sys.argv) >= 2 and sys.argv[1] in SELF_GROUPS:
            SELF_GROUPS[sys.argv[1]](sys.argv[2:])
            return 0
        main()
    except BloomeryError as e:
        print(f"\nX {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nX Interrupted.", file=sys.stderr)
        sys.exit(130)
    return 0
