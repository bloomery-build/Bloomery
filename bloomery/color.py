"""ANSI color for console output, on only when stdout is a real terminal
that supports it. NO_COLOR (https://no-color.org) always wins.
"""

import os
import sys

_CODES = {
    "green": "32",
    "yellow": "33",
    "red": "31",
    "cyan": "36",
    "dim": "2",
}


def _enable_windows_vt():
    """cmd.exe/PowerShell need ANSI escapes turned on explicitly."""
    if os.name != "nt":
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


def _detect_support():
    if os.environ.get("NO_COLOR"):
        return False
    if not sys.stdout.isatty():
        return False
    return _enable_windows_vt()


ENABLED = _detect_support()


def paint(text, color):
    if not ENABLED:
        return text
    return f"\033[{_CODES[color]}m{text}\033[0m"


def entry_line(primary, description="", entry_color='cyan'):
    """A colored primary token plus a dim description, pip-list style."""
    line = f"  {paint(primary, entry_color)}"
    if description:
        line += f"  {paint(description, 'dim')}"
    return line
