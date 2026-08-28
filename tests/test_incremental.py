"""Tests for header tracking, per-file builds, and parallel execution."""

import json
import time

import pytest

from bloomery import parse_depfile

from test_build import run_cli, write_toml

# A portable stand-in for a compiler: copies its inputs into the -o target
# so tests never depend on gcc being installed.
FAKE_CC = '''
import sys
args = sys.argv[1:]
out = args[args.index("-o") + 1]
srcs = [a for i, a in enumerate(args)
        if not a.startswith("-") and args[i - 1] != "-o"]
with open(out, "w") as f:
    for s in srcs:
        f.write(open(s).read())
'''


@pytest.fixture
def cc(tmp_path):
    path = tmp_path / "fake_cc.py"
    path.write_text(FAKE_CC, encoding="utf-8")
    return f'python "{path}"'


# ── depfile parsing ───────────────────────────────────────────────

def test_parse_depfile_basic(tmp_path):
    d = tmp_path / "a.d"
    d.write_text("obj/a.o: src/a.cpp include/v.h\n", encoding="utf-8")
    assert parse_depfile(str(d)) == ["src/a.cpp", "include/v.h"]


def test_parse_depfile_handles_line_continuations(tmp_path):
    d = tmp_path / "a.d"
    d.write_text("obj/a.o: src/a.cpp \\\n  include/v.h\n", encoding="utf-8")
    assert parse_depfile(str(d)) == ["src/a.cpp", "include/v.h"]


def test_parse_depfile_missing_file_is_empty():
    assert parse_depfile("does/not/exist.d") == []


# ── Headers field ─────────────────────────────────────────────────

def headers_toml(cc):
    return f"""
        [meta]
        name = "hdr"
        [variables]
        cc = '{cc}'
        [tasks.build]
        command = "{{cc}}"
        files = {{ ending = [".src"] }}
        headers = {{ ending = [".h"] }}
        output = "-o out.txt"
    """


def test_header_change_invalidates_the_cache(tmp_path, cc):
    (tmp_path / "a.src").write_text("body\n", encoding="utf-8")
    (tmp_path / "v.h").write_text("v1\n", encoding="utf-8")
    write_toml(tmp_path, headers_toml(cc))
    ini = str(tmp_path / "project.toml")

    assert "[RUN]" in run_cli(ini).stdout
    assert "[SKIP]" in run_cli(ini).stdout          # nothing changed

    (tmp_path / "v.h").write_text("v2\n", encoding="utf-8")
    assert "[RUN]" in run_cli(ini).stdout           # header edit rebuilds


def test_headers_are_not_placed_on_the_command_line(tmp_path, cc):
    (tmp_path / "a.src").write_text("body\n", encoding="utf-8")
    (tmp_path / "v.h").write_text("v1\n", encoding="utf-8")
    write_toml(tmp_path, headers_toml(cc))
    out = run_cli(str(tmp_path / "project.toml"), "--dry-run").stdout
    assert "a.src" in out
    assert "v.h" not in out


# ── per-file mode ─────────────────────────────────────────────────

def per_file_toml(cc):
    return f"""
        [meta]
        name = "pf"
        [variables]
        cc = '{cc}'
        [tasks.compile]
        mode = "per-file"
        command = "{{cc}}"
        files = {{ ending = [".src"] }}
        output = "-o obj/{{stem}}.o"
    """


def setup_per_file(tmp_path, cc, names=("a", "b")):
    for n in names:
        (tmp_path / f"{n}.src").write_text(f"{n}\n", encoding="utf-8")
    write_toml(tmp_path, per_file_toml(cc))
    return str(tmp_path / "project.toml")


def test_per_file_runs_one_command_per_file(tmp_path, cc):
    ini = setup_per_file(tmp_path, cc)
    out = run_cli(ini).stdout
    assert "compile [a.src]" in out
    assert "compile [b.src]" in out


def test_per_file_creates_output_directories(tmp_path, cc):
    ini = setup_per_file(tmp_path, cc)
    run_cli(ini)
    assert (tmp_path / "obj" / "a.o").exists()
    assert (tmp_path / "obj" / "b.o").exists()


def test_editing_one_source_recompiles_only_that_file(tmp_path, cc):
    ini = setup_per_file(tmp_path, cc)
    run_cli(ini)

    (tmp_path / "a.src").write_text("a changed\n", encoding="utf-8")
    out = run_cli(ini).stdout

    assert "[RUN]  compile [a.src]" in out
    assert "[SKIP] compile [b.src]" in out


def test_per_file_uses_one_cache_entry_per_file(tmp_path, cc):
    ini = setup_per_file(tmp_path, cc)
    run_cli(ini)
    data = json.loads((tmp_path / ".bloomery_cache.json").read_text())
    assert "compile:a.src" in data
    assert "compile:b.src" in data


def test_per_file_with_no_inputs_is_a_skip(tmp_path, cc):
    write_toml(tmp_path, per_file_toml(cc))
    out = run_cli(str(tmp_path / "project.toml")).stdout
    assert "no input files" in out


def test_task_outputs_feeds_a_link_step(tmp_path, cc):
    for n in ("a", "b"):
        (tmp_path / f"{n}.src").write_text(f"{n}\n", encoding="utf-8")
    write_toml(tmp_path, f"""
        [meta]
        name = "link"
        [variables]
        cc = '{cc}'
        [tasks.compile]
        mode = "per-file"
        command = "{{cc}}"
        files = {{ ending = [".src"] }}
        output = "-o obj/{{stem}}.o"
        [tasks.link]
        depends = ["compile"]
        command = "{{cc}}"
        files = "{{task.compile.outputs}}"
        output = "-o app.bin"
    """)
    r = run_cli(str(tmp_path / "project.toml"))
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "app.bin").read_text() == "a\nb\n"


# ── outputs field (compilers whose output flag isn't -o) ──────────

NONSTANDARD_CC = '''
import sys
args = sys.argv[1:]
out = next(a[len("--emit="):] for a in args if a.startswith("--emit="))
srcs = [a for a in args if not a.startswith("-")]
with open(out, "w") as f:
    for s in srcs:
        f.write(open(s).read())
'''


@pytest.fixture
def nonstandard_cc(tmp_path):
    path = tmp_path / "nonstandard_cc.py"
    path.write_text(NONSTANDARD_CC, encoding="utf-8")
    return f'python "{path}"'


def test_without_explicit_outputs_a_non_dash_o_flag_is_not_tracked(tmp_path, nonstandard_cc):
    """Documents the gap the 'outputs' field exists to close: with no
    explicit outputs, a deleted binary from a --emit=-style compiler is
    invisible to the cache, so a rebuild is wrongly skipped."""
    (tmp_path / "a.src").write_text("body\n", encoding="utf-8")
    write_toml(tmp_path, f"""
        [meta]
        name = "gap"
        [variables]
        cc = '{nonstandard_cc}'
        [tasks.build]
        command = "{{cc}}"
        files = {{ ending = [".src"] }}
        output = "--emit=out.bin"
    """)
    ini = str(tmp_path / "project.toml")
    run_cli(ini)
    (tmp_path / "out.bin").unlink()
    out = run_cli(ini).stdout
    assert "[SKIP]" in out          # wrongly considers the deleted binary current


def test_explicit_outputs_tracks_any_flag_syntax(tmp_path, nonstandard_cc):
    """The fix: declaring outputs explicitly makes cache tracking work
    regardless of what flag the compiler actually used."""
    (tmp_path / "a.src").write_text("body\n", encoding="utf-8")
    write_toml(tmp_path, f"""
        [meta]
        name = "fixed"
        [variables]
        cc = '{nonstandard_cc}'
        [tasks.build]
        command = "{{cc}}"
        files = {{ ending = [".src"] }}
        output = "--emit=out.bin"
        outputs = ["out.bin"]
    """)
    ini = str(tmp_path / "project.toml")
    run_cli(ini)
    assert "[SKIP]" in run_cli(ini).stdout      # unchanged: still cached

    (tmp_path / "out.bin").unlink()
    out = run_cli(ini).stdout
    assert "[RUN]" in out                        # missing output forces a rebuild
    assert (tmp_path / "out.bin").exists()


def test_explicit_outputs_supports_interpolation(tmp_path, nonstandard_cc):
    """outputs goes through the same resolver as files/headers, so {stem}
    and friends work inside it, not just literal strings."""
    (tmp_path / "a.src").write_text("body\n", encoding="utf-8")
    write_toml(tmp_path, f"""
        [meta]
        name = "stem"
        [variables]
        cc = '{nonstandard_cc}'
        [tasks.build]
        mode = "per-file"
        command = "{{cc}}"
        files = {{ ending = [".src"] }}
        output = "--emit=obj/{{stem}}.bin"
        outputs = ["obj/{{stem}}.bin"]
    """)
    run_cli(str(tmp_path / "project.toml"))
    assert (tmp_path / "obj" / "a.bin").exists()


# ── parallel execution ────────────────────────────────────────────

PARALLEL_TOML = """
    [meta]
    name = "par"
    [tasks.a]
    always_run = true
    command = 'python -c "import time;time.sleep(1)"'
    [tasks.b]
    always_run = true
    command = 'python -c "import time;time.sleep(1)"'
    [tasks.join]
    depends = ["a", "b"]
    always_run = true
    command = "python -c \\"print('joined')\\""
"""


def test_parallel_is_faster_than_serial(tmp_path):
    write_toml(tmp_path, PARALLEL_TOML)
    ini = str(tmp_path / "project.toml")

    start = time.monotonic()
    assert run_cli(ini).returncode == 0
    serial = time.monotonic() - start

    start = time.monotonic()
    assert run_cli(ini, "-j4").returncode == 0
    parallel = time.monotonic() - start

    assert parallel < serial - 0.4, f"serial={serial:.2f} parallel={parallel:.2f}"


def test_parallel_respects_dependencies(tmp_path):
    write_toml(tmp_path, PARALLEL_TOML)
    out = run_cli(str(tmp_path / "project.toml"), "-j4").stdout
    assert out.index("a") < out.index("joined")
    assert out.index("b") < out.index("joined")


def test_parallel_failure_stops_the_build(tmp_path):
    write_toml(tmp_path, """
        [meta]
        name = "fail"
        [tasks.a]
        always_run = true
        command = 'python -c "import sys;sys.exit(2)"'
        [tasks.b]
        always_run = true
        command = 'python -c "print(1)"'
        [tasks.join]
        depends = ["a", "b"]
        always_run = true
        command = "python -c \\"print('SHOULD_NOT_RUN')\\""
    """)
    r = run_cli(str(tmp_path / "project.toml"), "-j4")
    assert r.returncode == 1
    assert "SHOULD_NOT_RUN" not in r.stdout
    assert "Traceback" not in r.stderr


def test_jobs_zero_means_one_per_cpu(tmp_path):
    write_toml(tmp_path, PARALLEL_TOML)
    r = run_cli(str(tmp_path / "project.toml"), "-j0")
    assert r.returncode == 0
    assert "Jobs" in r.stdout


def test_serial_output_is_unchanged_by_default(tmp_path):
    write_toml(tmp_path, PARALLEL_TOML)
    out = run_cli(str(tmp_path / "project.toml")).stdout
    assert "-- a --" in out
    assert "Jobs" not in out
