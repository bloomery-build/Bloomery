"""Tests for the metaprogramming DSL (DirectiveEngine)."""

import os

import pytest

from bloomery import Context, DirectiveEngine


def engine(tmp_path=".", **variables):
    ctx = Context(project_dir=str(tmp_path), variables=variables)
    return DirectiveEngine(ctx)


# ── simple directives ─────────────────────────────────────────────

def test_var_directive():
    assert engine(compiler="g++").resolve("<var(compiler)>") == "g++"


def test_var_directive_missing_is_empty():
    assert engine().resolve("<var(nope)>") == ""


def test_env_directive(monkeypatch):
    monkeypatch.setenv("BLOOMERY_TEST_VAR", "yes")
    assert engine().resolve("<env(BLOOMERY_TEST_VAR)>") == "yes"


def test_platform_directive():
    result = engine().resolve("<platform>")
    assert result in ("windows", "linux", "macos")


def test_exists_directive(tmp_path):
    (tmp_path / "config.h").write_text("")
    eng = engine(tmp_path)
    assert eng.resolve("<exists(config.h)>") == "true"
    assert eng.resolve("<exists(absent.h)>") == "false"


def test_unknown_directive_is_left_alone():
    assert engine().resolve("<bogus(x)>") == "<bogus(x)>"


def test_register_handler_extends_the_engine():
    eng = engine()
    eng.register_handler("lib", lambda args: f"-l{args}")
    assert eng.resolve("<lib(boost)>") == "-lboost"


# ── interpolation ─────────────────────────────────────────────────

def test_interpolation():
    assert engine(compiler="g++").resolve("{compiler} -Wall") == "g++ -Wall"


def test_env_interpolation(monkeypatch):
    monkeypatch.setenv("BLOOMERY_USER", "kai")
    assert engine().resolve("{env.BLOOMERY_USER}_build") == "kai_build"


def test_interpolation_of_a_directive_valued_variable():
    """A variable holding a directive is resolved on a later pass."""
    eng = engine(debug="true", debug_flags="<if(var(debug))>-g<else>-O2<end>")
    assert eng.resolve("{debug_flags}") == "-g"


# ── conditionals ──────────────────────────────────────────────────

def test_if_on_variable_truthiness():
    assert engine(debug="true").resolve("<if(var(debug))>-g<else>-O2<end>") == "-g"
    assert engine(debug="false").resolve("<if(var(debug))>-g<else>-O2<end>") == "-O2"


@pytest.mark.parametrize("falsy", ["false", "0", "no", ""])
def test_falsy_variable_values(falsy):
    assert engine(debug=falsy).resolve("<if(var(debug))>y<else>n<end>") == "n"


def test_if_on_equality():
    eng = engine(mode="safe")
    assert eng.resolve("<if(var(mode)=safe)>-fsanitize=address<end>") == "-fsanitize=address"
    assert eng.resolve("<if(var(mode)=fast)>-Ofast<end>") == ""


def test_if_on_inequality():
    assert engine(mode="safe").resolve("<if(var(mode)!=fast)>y<end>") == "y"


def test_negation():
    assert engine(debug="false").resolve("<if(!var(debug))>-DNDEBUG<end>") == "-DNDEBUG"
    assert engine(debug="true").resolve("<if(!var(debug))>-DNDEBUG<end>") == ""


def test_platform_condition():
    eng = engine()
    current = eng.ctx.platform
    assert eng.resolve(f"<if(platform={current})>hit<else>miss<end>") == "hit"
    assert eng.resolve(f"<if(platform!={current})>hit<else>miss<end>") == "miss"


def test_elif_chain():
    for level, expected in [("1", "-O1"), ("2", "-O2"), ("3", "-O3"), ("9", "-O0")]:
        eng = engine(level=level)
        out = eng.resolve(
            "<if(var(level)=1)>-O1<elif(var(level)=2)>-O2"
            "<elif(var(level)=3)>-O3<else>-O0<end>"
        )
        assert out == expected


def test_nested_if_blocks():
    """The inner <end> must not close the outer <if>."""
    eng = engine(a="true", b="true")
    out = eng.resolve("<if(var(a))>A<if(var(b))>B<end><end>")
    assert out == "AB"


def test_nested_if_taking_the_else_branch():
    eng = engine(a="true", b="false")
    out = eng.resolve("<if(var(a))>A<if(var(b))>B<else>C<end><end>")
    assert out == "AC"


def test_if_condition_with_nested_parens_is_tokenized():
    """<if(var(debug))> has a paren inside the condition — must balance."""
    assert engine(debug="true").resolve("<if(var(debug))>ok<end>") == "ok"


def test_exists_condition(tmp_path):
    (tmp_path / "config.h").write_text("")
    eng = engine(tmp_path)
    assert eng.resolve("<if(exists(config.h))>-DHAVE_CONFIG<end>") == "-DHAVE_CONFIG"
    assert eng.resolve("<if(exists(absent.h))>-DHAVE_CONFIG<end>") == ""


def test_env_condition(monkeypatch):
    monkeypatch.delenv("BLOOMERY_CI", raising=False)
    assert engine().resolve("<if(env(BLOOMERY_CI))>-DCI<end>") == ""
    monkeypatch.setenv("BLOOMERY_CI", "1")
    assert engine().resolve("<if(env(BLOOMERY_CI))>-DCI<end>") == "-DCI"


# ── loops ─────────────────────────────────────────────────────────

def test_for_over_literal_list():
    assert engine().resolve("<for(d in DEBUG|VERBOSE)>-D{d}<end>") == "-DDEBUG -DVERBOSE"


def test_for_over_range():
    assert engine().resolve("<for(i in range(1,4))>-DID_{i}<end>") == "-DID_1 -DID_2 -DID_3"


def test_for_over_files(tmp_path):
    (tmp_path / "a.cpp").write_text("")
    (tmp_path / "b.cpp").write_text("")
    (tmp_path / "skip.txt").write_text("")
    out = engine(tmp_path).resolve("<for(f in files: .cpp)>{f}<end>")
    assert out.split() == ["a.cpp", "b.cpp"]


def test_for_restores_the_shadowed_variable():
    eng = engine(x="original")
    eng.resolve("<for(x in a|b)>{x}<end>")
    assert eng.ctx.get_var("x") == "original"


def test_for_removes_the_loop_variable_when_it_was_unset():
    eng = engine()
    eng.resolve("<for(y in a|b)>{y}<end>")
    assert "y" not in eng.ctx.variables


def test_if_nested_inside_for():
    eng = engine(debug="true")
    out = eng.resolve("<for(d in A|B)><if(var(debug))>-D{d}<end><end>")
    assert out == "-DA -DB"


# ── file resolution ───────────────────────────────────────────────

def test_files_ending(tmp_path):
    (tmp_path / "a.cpp").write_text("")
    (tmp_path / "b.c").write_text("")
    (tmp_path / "readme.md").write_text("")
    out = engine(tmp_path).resolve("<files ending (.cpp|.c)>")
    assert sorted(out.split()) == ["a.cpp", "b.c"]


def test_files_matching_is_recursive(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.cpp").write_text("")
    out = engine(tmp_path).resolve("<files matching (src/**.cpp)>")
    assert out == os.path.join("src", "main.cpp")


def test_files_in_directory(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "util.cpp").write_text("")
    (tmp_path / "root.cpp").write_text("")
    out = engine(tmp_path).resolve("<files in (lib; .cpp)>")
    assert out == os.path.join("lib", "util.cpp")


# ── comments ──────────────────────────────────────────────────────

def test_inline_comment_is_stripped():
    assert engine().resolve("g++ -Wall ; a comment").strip() == "g++ -Wall"


def test_unspaced_semicolon_is_not_a_comment():
    """Only a *space-delimited* ' ; ' starts a comment, so 'a; b' survives."""
    assert engine().resolve("cd dir; make") == "cd dir; make"


def test_spaced_semicolon_shell_separator_is_eaten_known_limitation():
    """Known limitation: ' ; ' is always read as a comment marker.

    A spaced shell separator therefore loses everything after it.  Use
    'cd dir; make' or '&&' instead.  Pinned here so the behaviour cannot
    change silently.
    """
    assert engine().resolve("cd dir ; make") == "cd dir"
