"""Tests for header tracking, per-file builds, and parallel execution."""

import json
import os
import textwrap
import time

import pytest

from bloomery import BuildCache, Context, DirectiveEngine, TaskDAG, parse_depfile, parse_ini

from test_build import run_cli, write_ini

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
    d.write_text("obj/a.o: src/a.cpp \\\n  include/v.h \\\n  include/w.h\n",
                 encoding="utf-8")
    assert parse_depfile(str(d)) == ["src/a.cpp", "include/v.h", "include/w.h"]


def test_parse_depfile_deduplicates(tmp_path):
    d = tmp_path / "a.d"
    d.write_text("a.o: x.h x.h y.h\n", encoding="utf-8")
    assert parse_depfile(str(d)) == ["x.h", "y.h"]


def test_parse_depfile_missing_file_is_empty():
    assert parse_depfile("does/not/exist.d") == []


def test_parse_depfile_ignores_lines_without_a_colon(tmp_path):
    d = tmp_path / "a.d"
    d.write_text("just noise\nobj/a.o: src/a.cpp\n", encoding="utf-8")
    assert parse_depfile(str(d)) == ["src/a.cpp"]


# ── cache deps ────────────────────────────────────────────────────

def test_cache_stores_and_returns_deps(tmp_path):
    cache = BuildCache(str(tmp_path))
    cache.update("Build", "h", outputs=["a.o"], deps=["v.h"])
    assert cache.get_deps("Build") == ["v.h"]


def test_get_deps_on_a_legacy_entry(tmp_path):
    cache = BuildCache(str(tmp_path))
    cache.data["Build"] = "bare-hash-string"
    assert cache.get_deps("Build") == []


# ── Headers field ─────────────────────────────────────────────────

HEADERS_INI = """
    [Meta]
    Name = hdr
    [Variables]
    cc = @CC@
    [Tasks]
    build = Build
    [Tasks.Build]
    Command = {cc}
    Files = <files ending (.src)>
    Headers = <files ending (.h)>
    Output = -o out.txt
"""


def test_header_change_invalidates_the_cache(tmp_path, cc):
    (tmp_path / "a.src").write_text("body\n", encoding="utf-8")
    (tmp_path / "v.h").write_text("v1\n", encoding="utf-8")
    write_ini(tmp_path, HEADERS_INI.replace("@CC@", cc))
    ini = str(tmp_path / "project.ini")

    assert "[RUN]" in run_cli(ini).stdout
    assert "[SKIP]" in run_cli(ini).stdout          # nothing changed

    (tmp_path / "v.h").write_text("v2\n", encoding="utf-8")
    assert "[RUN]" in run_cli(ini).stdout           # header edit rebuilds


def test_headers_are_not_placed_on_the_command_line(tmp_path, cc):
    (tmp_path / "a.src").write_text("body\n", encoding="utf-8")
    (tmp_path / "v.h").write_text("v1\n", encoding="utf-8")
    write_ini(tmp_path, HEADERS_INI.replace("@CC@", cc))
    out = run_cli(str(tmp_path / "project.ini"), "--dry-run").stdout
    assert "a.src" in out
    assert "v.h" not in out


# ── per-file mode ─────────────────────────────────────────────────

PER_FILE_INI = """
    [Meta]
    Name = pf
    [Variables]
    cc = @CC@
    [Tasks]
    build = Compile
    [Tasks.Compile]
    Mode = per-file
    Command = {cc}
    Files = <files ending (.src)>
    Output = -o obj/<stem>.o
"""


def setup_per_file(tmp_path, cc, names=("a", "b")):
    for n in names:
        (tmp_path / f"{n}.src").write_text(f"{n}\n", encoding="utf-8")
    write_ini(tmp_path, PER_FILE_INI.replace("@CC@", cc))
    return str(tmp_path / "project.ini")


def test_per_file_runs_one_command_per_file(tmp_path, cc):
    ini = setup_per_file(tmp_path, cc)
    out = run_cli(ini).stdout
    assert "Compile [a.src]" in out
    assert "Compile [b.src]" in out


def test_per_file_creates_output_directories(tmp_path, cc):
    ini = setup_per_file(tmp_path, cc)
    run_cli(ini)
    assert (tmp_path / "obj" / "a.o").exists()
    assert (tmp_path / "obj" / "b.o").exists()


def test_stem_advances_per_file(tmp_path, cc):
    ini = setup_per_file(tmp_path, cc)
    out = run_cli(ini, "--dry-run").stdout
    assert "obj/a.o" in out
    assert "obj/b.o" in out


def test_editing_one_source_recompiles_only_that_file(tmp_path, cc):
    ini = setup_per_file(tmp_path, cc)
    run_cli(ini)

    (tmp_path / "a.src").write_text("a changed\n", encoding="utf-8")
    out = run_cli(ini).stdout

    assert "[RUN]  Compile [a.src]" in out
    assert "[SKIP] Compile [b.src]" in out


def test_per_file_uses_one_cache_entry_per_file(tmp_path, cc):
    ini = setup_per_file(tmp_path, cc)
    run_cli(ini)
    data = json.loads((tmp_path / ".bloomery_cache.json").read_text())
    assert "Compile:a.src" in data
    assert "Compile:b.src" in data


def test_per_file_with_no_inputs_is_a_skip(tmp_path, cc):
    write_ini(tmp_path, PER_FILE_INI.replace("@CC@", cc))
    out = run_cli(str(tmp_path / "project.ini")).stdout
    assert "no input files" in out


def test_task_outputs_interpolation_feeds_a_link_step(tmp_path, cc):
    for n in ("a", "b"):
        (tmp_path / f"{n}.src").write_text(f"{n}\n", encoding="utf-8")
    write_ini(tmp_path, textwrap.dedent(f"""
        [Meta]
        Name = link
        [Tasks]
        build = Link
        [Tasks.Compile]
        Mode = per-file
        Command = {cc}
        Files = <files ending (.src)>
        Output = -o obj/<stem>.o
        [Tasks.Link]
        Depends = Compile
        Command = {cc}
        Files = {{task.Compile.outputs}}
        Output = -o app.bin
    """))
    r = run_cli(str(tmp_path / "project.ini"))
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "app.bin").read_text() == "a\nb\n"


# ── DAG waves ─────────────────────────────────────────────────────

def waves_for(tmp_path, body):
    config = parse_ini(write_ini(tmp_path, body))
    dag = TaskDAG().build(config)
    order = dag.topo_sort_with_config(config)
    return [set(w) for w in dag.ready_waves(config, order)]


def test_independent_tasks_share_a_wave(tmp_path):
    waves = waves_for(tmp_path, """
        [Tasks]
        a = A
        b = B
        j = Join
        [Tasks.A]
        Command = echo a
        [Tasks.B]
        Command = echo b
        [Tasks.Join]
        Depends = A, B
        Command = echo j
    """)
    assert waves == [{"A", "B"}, {"Join"}]


def test_a_chain_is_one_task_per_wave(tmp_path):
    waves = waves_for(tmp_path, """
        [Tasks]
        a = A
        b = B
        c = C
        [Tasks.A]
        Command = echo a
        [Tasks.B]
        Depends = A
        Command = echo b
        [Tasks.C]
        Depends = B
        Command = echo c
    """)
    assert waves == [{"A"}, {"B"}, {"C"}]


# ── Context / engine forking ──────────────────────────────────────

def test_fork_isolates_scratch_variables():
    ctx = Context(variables={"shared": "1"})
    clone = ctx.fork()
    clone.set_var("input", "mine")
    assert ctx.get_var("input") == ""
    assert clone.get_var("shared") == "1"


def test_fork_shares_resolved_outputs():
    """A dependent task must see what its dependency produced."""
    ctx = Context()
    clone = ctx.fork()
    clone.resolved_outputs["Compile"] = ["a.o"]
    assert ctx.resolved_outputs["Compile"] == ["a.o"]


def test_forked_engine_keeps_custom_handlers():
    eng = DirectiveEngine(Context())
    eng.register_handler("lib", lambda args: f"-l{args}")
    forked = eng._fork_engine(eng.ctx.fork())
    assert forked.resolve("<lib(boost)>") == "-lboost"


# ── parallel execution ────────────────────────────────────────────

PARALLEL_INI = """
    [Meta]
    Name = par
    [Tasks]
    all = Join
    [Tasks.A]
    AlwaysRun = yes
    Command = python -c "import time;time.sleep(1)"
    [Tasks.B]
    AlwaysRun = yes
    Command = python -c "import time;time.sleep(1)"
    [Tasks.Join]
    Depends = A, B
    AlwaysRun = yes
    Command = python -c "print('joined')"
"""


def test_parallel_is_faster_than_serial(tmp_path):
    write_ini(tmp_path, PARALLEL_INI)
    ini = str(tmp_path / "project.ini")

    start = time.monotonic()
    assert run_cli(ini).returncode == 0
    serial = time.monotonic() - start

    start = time.monotonic()
    assert run_cli(ini, "-j4").returncode == 0
    parallel = time.monotonic() - start

    # Two 1s sleeps: serial >= 2s, parallel should overlap them.
    assert parallel < serial - 0.4, f"serial={serial:.2f} parallel={parallel:.2f}"


def test_parallel_respects_dependencies(tmp_path):
    write_ini(tmp_path, PARALLEL_INI)
    out = run_cli(str(tmp_path / "project.ini"), "-j4").stdout
    assert out.index("A") < out.index("joined")
    assert out.index("B") < out.index("joined")


def test_parallel_failure_stops_the_build(tmp_path):
    write_ini(tmp_path, """
        [Meta]
        Name = fail
        [Tasks]
        all = Join
        [Tasks.A]
        AlwaysRun = yes
        Command = python -c "import sys;sys.exit(2)"
        [Tasks.B]
        AlwaysRun = yes
        Command = python -c "print('B ok')"
        [Tasks.Join]
        Depends = A, B
        AlwaysRun = yes
        Command = python -c "print('SHOULD NOT RUN')"
    """)
    r = run_cli(str(tmp_path / "project.ini"), "-j4")
    assert r.returncode == 1
    assert "SHOULD NOT RUN" not in r.stdout
    assert "Traceback" not in r.stderr


def test_jobs_zero_means_one_per_cpu(tmp_path):
    write_ini(tmp_path, PARALLEL_INI)
    r = run_cli(str(tmp_path / "project.ini"), "-j0")
    assert r.returncode == 0
    assert "Jobs" in r.stdout


def test_serial_output_is_unchanged_by_default(tmp_path):
    """Without -j the run stays single-threaded and prints task banners."""
    write_ini(tmp_path, PARALLEL_INI)
    out = run_cli(str(tmp_path / "project.ini")).stdout
    assert "-- A --" in out
    assert "Jobs" not in out
