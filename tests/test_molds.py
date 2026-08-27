"""Tests for mold discovery, inheritance, and namespacing."""

import os

import pytest

from bloomery import Context, Evaluator, MoldNotFoundError, load_mold, mold_search_path, parse_toml

BUNDLED = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bloomery", "molds")


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return path


# ── discovery ─────────────────────────────────────────────────────

def test_bundled_molds_ship_with_the_package():
    names = {f.lower() for f in os.listdir(BUNDLED)}
    assert {"c.toml", "c++.toml", "rust.toml", "python.toml"} <= names


def test_bundled_mold_is_found_without_a_local_copy(tmp_path):
    mold = load_mold("c", str(tmp_path))
    assert mold is not None
    assert mold["definitions"]["compiler"] == "gcc"


def test_project_directory_wins_over_the_bundled_mold(tmp_path):
    write(tmp_path / "c.toml", """
        [definitions]
        compiler = "my-local-cc"
    """)
    mold = load_mold("c", str(tmp_path))
    assert mold["definitions"]["compiler"] == "my-local-cc"


def test_env_var_extends_the_search_path(tmp_path, monkeypatch):
    shared = tmp_path / "shared"
    write(shared / "zig.toml", """
        [definitions]
        compiler = "zig"
    """)
    project = tmp_path / "project"
    project.mkdir()

    with pytest.raises(MoldNotFoundError):
        load_mold("zig", str(project))

    monkeypatch.setenv("BLOOMERY_MOLD_PATH", str(shared))
    assert load_mold("zig", str(project))["definitions"]["compiler"] == "zig"


def test_meta_mold_path_extends_the_search_path(tmp_path):
    write(tmp_path / "molds" / "zig.toml", """
        [definitions]
        compiler = "zig"
    """)
    config = parse_toml(write(tmp_path / "p.toml", """
        [meta]
        system = "zig"
        mold_path = "molds"
    """))
    assert load_mold("zig", str(tmp_path), config)["definitions"]["compiler"] == "zig"


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
    write(tmp_path / "base.toml", """
        [definitions]
        compiler = "base-cc"
        standard = "11"
    """)
    write(tmp_path / "child.toml", """
        [bloomery]
        extends = "base"

        [definitions]
        compiler = "child-cc"
    """)
    defs = load_mold("child", str(tmp_path))["definitions"]
    assert defs["compiler"] == "child-cc"     # child wins
    assert defs["standard"] == "11"           # inherited


def test_cyclic_inheritance_is_rejected(tmp_path):
    write(tmp_path / "a.toml", """
        [bloomery]
        extends = "b"
        [definitions]
        x = "1"
    """)
    write(tmp_path / "b.toml", """
        [bloomery]
        extends = "a"
        [definitions]
        y = "2"
    """)
    with pytest.raises(MoldNotFoundError, match="Cyclic"):
        load_mold("a", str(tmp_path))


# ── namespacing / {mold.X} ─────────────────────────────────────────

def evaluator_with_mold(tmp_path, mold_text, **variables):
    write(tmp_path / "m.toml", mold_text)
    ctx = Context(
        project_dir=str(tmp_path),
        variables=variables,
        mold_config=load_mold("m", str(tmp_path)),
    )
    return Evaluator(ctx)


def test_mold_field_interpolation(tmp_path):
    e = evaluator_with_mold(tmp_path, """
        [definitions]
        compiler = "g++"
    """)
    assert e.resolve_str("{mold.compiler}") == "g++"


def test_mold_definitions_do_not_leak_into_variables(tmp_path):
    """A mold key must not shadow a project variable of the same name."""
    e = evaluator_with_mold(tmp_path, """
        [definitions]
        compiler = "mold-cc"
    """, compiler="project-cc")
    assert e.resolve_str("{compiler}") == "project-cc"
    assert e.resolve_str("{mold.compiler}") == "mold-cc"


def test_mold_definitions_cross_reference_via_mold_prefix(tmp_path):
    """A mold definition references a sibling definition, not a bare name."""
    e = evaluator_with_mold(tmp_path, """
        [definitions]
        standard = "17"
        flags = "-std=c++{mold.standard}"
    """)
    assert e.resolve_str("{mold.flags}") == "-std=c++17"


def test_missing_mold_field_is_empty(tmp_path):
    e = evaluator_with_mold(tmp_path, """
        [definitions]
        compiler = "g++"
    """)
    assert e.resolve_str("{mold.nope}") == ""


def test_mold_field_can_be_a_dispatch_table(tmp_path):
    e = evaluator_with_mold(tmp_path, """
        [definitions]
        ext = { on = "platform", windows = ".exe", default = "" }
    """, platform="windows")
    assert e.resolve_str("{mold.ext}") == ".exe"


# ── all four bundled molds parse and expose a compiler ────────────

@pytest.mark.parametrize("system,expected_compiler", [
    ("c", "gcc"),
    ("c++", "g++"),
    ("rust", "rustc"),
])
def test_bundled_mold_compiler(tmp_path, system, expected_compiler):
    mold = load_mold(system, str(tmp_path))
    assert mold["definitions"]["compiler"] == expected_compiler


def test_bundled_python_mold_dispatches_compiler_by_platform(tmp_path):
    mold = load_mold("python", str(tmp_path))
    compiler = mold["definitions"]["compiler"]
    assert compiler == {"on": "platform", "windows": "python", "default": "python3"}
