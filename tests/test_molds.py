"""Tests for mold discovery, inheritance, and namespacing."""

import os

import pytest

from bloomery import (
    Context,
    DirectiveEngine,
    MoldNotFoundError,
    load_mold,
    mold_search_path,
    parse_ini,
)

BUNDLED = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bloomery", "molds")


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return path


# ── discovery ─────────────────────────────────────────────────────

def test_bundled_molds_ship_with_the_package():
    names = {f.lower() for f in os.listdir(BUNDLED)}
    assert {"c.ini", "c++.ini", "rust.ini", "python.ini"} <= names


def test_bundled_mold_is_found_without_a_local_copy(tmp_path):
    mold = load_mold("c", str(tmp_path))
    assert mold is not None
    assert mold["Definitions"]["Compiler"] == "gcc"


def test_project_directory_wins_over_the_bundled_mold(tmp_path):
    write(tmp_path / "c.ini", """
        [Definitions]
        Compiler = my-local-cc
    """)
    mold = load_mold("c", str(tmp_path))
    assert mold["Definitions"]["Compiler"] == "my-local-cc"


def test_env_var_extends_the_search_path(tmp_path, monkeypatch):
    shared = tmp_path / "shared"
    write(shared / "zig.ini", """
        [Definitions]
        Compiler = zig
    """)
    project = tmp_path / "project"
    project.mkdir()

    with pytest.raises(MoldNotFoundError):
        load_mold("zig", str(project))

    monkeypatch.setenv("BLOOMERY_MOLD_PATH", str(shared))
    assert load_mold("zig", str(project))["Definitions"]["Compiler"] == "zig"


def test_meta_moldpath_extends_the_search_path(tmp_path):
    write(tmp_path / "molds" / "zig.ini", """
        [Definitions]
        Compiler = zig
    """)
    config = parse_ini(write(tmp_path / "p.ini", """
        [Meta]
        System = zig
        MoldPath = molds
    """))
    assert load_mold("zig", str(tmp_path), config)["Definitions"]["Compiler"] == "zig"


def test_missing_mold_raises_and_lists_searched_paths(tmp_path):
    with pytest.raises(MoldNotFoundError) as exc:
        load_mold("nosuchlang", str(tmp_path))
    message = str(exc.value)
    assert "nosuchlang" in message
    assert "Searched:" in message


def test_no_system_means_no_mold(tmp_path):
    assert load_mold("", str(tmp_path)) is None


def test_search_path_prefers_the_project_directory(tmp_path):
    paths = list(mold_search_path(str(tmp_path)))
    assert paths[0] == str(tmp_path)
    assert paths[-1] == BUNDLED


# ── inheritance ───────────────────────────────────────────────────

def test_extends_inherits_missing_definitions(tmp_path):
    write(tmp_path / "base.ini", """
        [Definitions]
        Compiler = base-cc
        Standard = 11
    """)
    write(tmp_path / "child.ini", """
        [Bloomery]
        Extends = base

        [Definitions]
        Compiler = child-cc
    """)
    defs = load_mold("child", str(tmp_path))["Definitions"]
    assert defs["Compiler"] == "child-cc"     # child wins
    assert defs["Standard"] == "11"           # inherited


def test_cyclic_inheritance_is_rejected(tmp_path):
    write(tmp_path / "a.ini", """
        [Bloomery]
        Extends = b
        [Definitions]
        X = 1
    """)
    write(tmp_path / "b.ini", """
        [Bloomery]
        Extends = a
        [Definitions]
        Y = 2
    """)
    with pytest.raises(MoldNotFoundError, match="Cyclic"):
        load_mold("a", str(tmp_path))


# ── namespacing ───────────────────────────────────────────────────

def engine_with_mold(tmp_path, mold_text, **variables):
    write(tmp_path / "m.ini", mold_text)
    ctx = Context(
        project_dir=str(tmp_path),
        variables=variables,
        mold_config=load_mold("m", str(tmp_path)),
    )
    return DirectiveEngine(ctx)


def test_mold_field_directive(tmp_path):
    eng = engine_with_mold(tmp_path, """
        [Definitions]
        Compiler = g++
    """)
    assert eng.resolve("<mold(Compiler)>") == "g++"


def test_mold_interpolation_prefix(tmp_path):
    eng = engine_with_mold(tmp_path, """
        [Definitions]
        Compiler = g++
    """)
    assert eng.resolve("{mold.Compiler} -Wall") == "g++ -Wall"


def test_mold_definitions_do_not_leak_into_variables(tmp_path):
    """A mold key must not shadow a project variable of the same name."""
    eng = engine_with_mold(tmp_path, """
        [Definitions]
        Compiler = mold-cc
    """, Compiler="project-cc")
    assert eng.resolve("{Compiler}") == "project-cc"
    assert eng.resolve("<mold(Compiler)>") == "mold-cc"


def test_mold_sees_its_own_definitions_while_expanding(tmp_path):
    """'-std=c<var(standard)>' must resolve using the mold's own Standard."""
    eng = engine_with_mold(tmp_path, """
        [Definitions]
        Standard = 17
        Flags = -std=c++<var(standard)>
    """)
    assert eng.resolve("<mold(Flags)>") == "-std=c++17"
    # ...and the alias is gone afterwards
    assert eng.ctx.get_var("standard") == ""


def test_mold_none_is_empty(tmp_path):
    eng = engine_with_mold(tmp_path, """
        [Definitions]
        Files = a.cpp
    """)
    assert eng.resolve("<mold(none)>").strip() == ""


def test_bare_mold_uses_the_current_field(tmp_path):
    eng = engine_with_mold(tmp_path, """
        [Definitions]
        Flags = -O2
    """)
    assert eng.resolve("<mold()> -Wall", field_name="Flags") == "-O2 -Wall"
