"""Tests for the Evaluator: interpolation, dispatch tables, reserved keys."""

import os

import pytest

from bloomery import BloomeryError, Context, Evaluator


def ev(tmp_path=".", **variables):
    ctx = Context(project_dir=str(tmp_path), variables=variables)
    return Evaluator(ctx)


# ── plain interpolation ─────────────────────────────────────────

def test_bare_reference():
    assert ev(compiler="g++").resolve_str("{compiler}") == "g++"


def test_missing_reference_is_empty():
    assert ev().resolve_str("{nope}") == ""


def test_env_reference(monkeypatch):
    monkeypatch.setenv("BLOOMERY_TEST", "yes")
    assert ev().resolve_str("{env.BLOOMERY_TEST}") == "yes"


def test_platform_is_seeded_as_a_variable():
    result = ev().resolve_str("{platform}")
    assert result in ("windows", "linux", "macos")


def test_bool_variable_stringifies_lowercase():
    assert ev(debug=True).resolve_str("{debug}") == "true"
    assert ev(debug=False).resolve_str("{debug}") == "false"


def test_string_around_interpolation_is_preserved():
    assert ev(compiler="g++").resolve_str("{compiler} -Wall") == "g++ -Wall"


def test_resolve_str_of_a_dispatch_valued_variable():
    """A named variable can itself hold a dispatch table."""
    e = ev(debug=True)
    e.ctx.set_var("flags", {"on": "debug", "true": "-g", "false": "-O2"})
    assert e.resolve_str("{flags}") == "-g"


# ── cycle detection ──────────────────────────────────────────────

def test_self_reference_raises():
    e = ev()
    e.ctx.set_var("a", "{a}")
    with pytest.raises(BloomeryError, match="Cyclic"):
        e.resolve_str("{a}")


def test_mutual_reference_raises():
    e = ev()
    e.ctx.set_var("a", "{b}")
    e.ctx.set_var("b", "{a}")
    with pytest.raises(BloomeryError, match="Cyclic"):
        e.resolve_str("{a}")


def test_diamond_reference_is_not_a_false_cycle():
    """a and b both reference c — not cyclic, just shared."""
    e = ev()
    e.ctx.set_var("a", "{c}-x")
    e.ctx.set_var("b", "{c}-y")
    e.ctx.set_var("c", "shared")
    assert e.resolve_str("{a} {b}") == "shared-x shared-y"


# ── dispatch tables ────────────────────────────────────────────

def test_dispatch_on_bool():
    table = {"on": "debug", "true": "-g -O0", "false": "-O2"}
    assert ev(debug=True).resolve_str(table) == "-g -O0"
    assert ev(debug=False).resolve_str(table) == "-O2"


def test_dispatch_on_string():
    table = {"on": "platform", "windows": ".exe", "default": ""}
    assert ev(platform="windows").resolve_str(table) == ".exe"
    assert ev(platform="linux").resolve_str(table) == ""


def test_dispatch_falls_back_to_default():
    table = {"on": "mode", "safe": "-fsanitize=address", "default": "-O2"}
    assert ev(mode="fast").resolve_str(table) == "-O2"


def test_dispatch_with_no_match_and_no_default_is_empty():
    table = {"on": "mode", "safe": "-fsanitize=address"}
    assert ev(mode="fast").resolve_str(table) == ""


def test_dispatch_branch_may_itself_be_a_dispatch():
    table = {
        "on": "debug",
        "true": {"on": "platform", "windows": "-Zdbg", "default": "-g"},
        "false": "-O2",
    }
    assert ev(debug=True, platform="windows").resolve_str(table) == "-Zdbg"
    assert ev(debug=True, platform="linux").resolve_str(table) == "-g"
    assert ev(debug=False, platform="windows").resolve_str(table) == "-O2"


def test_dispatch_branch_may_be_a_list():
    table = {"on": "debug", "true": ["-g", "-O0"], "false": ["-O2"]}
    assert ev(debug=True).resolve_list(table) == ["-g", "-O0"]


def test_cli_override_is_a_string_and_matches_string_dispatch_keys():
    """-D debug=false always produces a str; dispatch keys are str too."""
    table = {"on": "debug", "true": "-g", "false": "-O2"}
    assert ev(debug="false").resolve_str(table) == "-O2"


# ── reserved-key tables: files ────────────────────────────────

def test_files_ending(tmp_path):
    (tmp_path / "a.cpp").write_text("")
    (tmp_path / "b.c").write_text("")
    (tmp_path / "readme.md").write_text("")
    out = ev(tmp_path).resolve_list({"ending": [".cpp", ".c"]})
    assert sorted(out) == ["a.cpp", "b.c"]


def test_files_ending_in_a_directory(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "util.cpp").write_text("")
    (tmp_path / "root.cpp").write_text("")
    out = ev(tmp_path).resolve_list({"in": "lib", "ending": [".cpp"]})
    assert out == [os.path.join("lib", "util.cpp")]


def test_files_matching_is_recursive(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.cpp").write_text("")
    out = ev(tmp_path).resolve_list({"matching": "src/**.cpp"})
    assert out == [os.path.join("src", "main.cpp")]


# ── reserved-key tables: exists / shell / prefix ──────────────

def test_exists(tmp_path):
    (tmp_path / "config.h").write_text("")
    e = ev(tmp_path)
    assert e.resolve_str({"exists": "config.h"}) == "true"
    assert e.resolve_str({"exists": "absent.h"}) == "false"


def test_shell_captures_stdout(tmp_path):
    out = ev(tmp_path).resolve_str({"shell": "echo hello"})
    assert out == "hello"


def test_shell_failure_is_empty(tmp_path):
    out = ev(tmp_path).resolve_str({"shell": "definitely-not-a-real-command-xyz"})
    assert out == ""


def test_prefix_items_sugar():
    table = {"prefix": "-D", "items": ["DEBUG", "VERBOSE"]}
    assert ev().resolve_str(table) == "-DDEBUG -DVERBOSE"
    assert ev().resolve_list(table) == ["-DDEBUG", "-DVERBOSE"]


def test_prefix_items_resolves_each_item():
    """Items are themselves interpolated before the prefix is applied."""
    e = ev(name="X")
    table = {"prefix": "-D", "items": ["{name}", "OTHER"]}
    assert e.resolve_str(table) == "-DX -DOTHER"


def test_unrecognized_table_shape_raises():
    with pytest.raises(BloomeryError, match="Unrecognized table"):
        ev().resolve_str({"nonsense": 1})


# ── resolve_str vs resolve_list ────────────────────────────────

def test_resolve_str_joins_a_list_with_spaces():
    assert ev().resolve_str(["a", "b", "c"]) == "a b c"


def test_resolve_list_splits_a_plain_string():
    assert ev().resolve_list("a b  c") == ["a", "b", "c"]


def test_resolve_list_of_empty_string_is_empty():
    assert ev().resolve_list("") == []


def test_resolve_list_of_missing_field_default_is_empty():
    assert ev().resolve_list([]) == []
