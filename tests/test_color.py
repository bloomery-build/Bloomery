"""Tests for ANSI color support detection and painting."""

import pytest

from bloomery import color


def test_paint_wraps_text_in_ansi_codes_when_enabled(monkeypatch):
    monkeypatch.setattr(color, "ENABLED", True)
    assert color.paint("[RUN]", "green") == "\033[32m[RUN]\033[0m"


def test_paint_returns_plain_text_when_disabled(monkeypatch):
    monkeypatch.setattr(color, "ENABLED", False)
    assert color.paint("[RUN]", "green") == "[RUN]"


def test_paint_preserves_the_original_text_as_a_substring(monkeypatch):
    """Callers/tests that grep for a bare tag must still find it."""
    monkeypatch.setattr(color, "ENABLED", True)
    assert "[SKIP]" in color.paint("[SKIP]", "yellow")


def test_no_color_env_var_disables_support(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(color.sys.stdout, "isatty", lambda: True)
    assert color._detect_support() is False


def test_non_tty_disables_support(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(color.sys.stdout, "isatty", lambda: False)
    assert color._detect_support() is False


def test_tty_without_no_color_enables_support_on_posix(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(color.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(color, "_enable_windows_vt", lambda: True)
    assert color._detect_support() is True


def test_entry_line_colors_name_and_description_separately(monkeypatch):
    monkeypatch.setattr(color, "ENABLED", True)
    line = color.entry_line("c++", "Default C++ compilation mold")
    assert line == "  \033[36mc++\033[0m  \033[2mDefault C++ compilation mold\033[0m"


def test_entry_line_omits_the_description_segment_when_blank(monkeypatch):
    monkeypatch.setattr(color, "ENABLED", True)
    assert color.entry_line("c++", "") == "  \033[36mc++\033[0m"


def test_entry_line_plain_text_preserves_both_fields(monkeypatch):
    monkeypatch.setattr(color, "ENABLED", False)
    assert color.entry_line("c++", "Default C++ compilation mold") == "  c++  Default C++ compilation mold"
