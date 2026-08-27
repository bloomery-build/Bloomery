"""Tests for the task graph, build cache, TOML helpers, and the CLI."""

import os
import subprocess
import sys
import textwrap

import pytest

from bloomery import (
    BuildCache,
    CyclicDependencyError,
    TaskDAG,
    load_profiles,
    load_variables,
    parse_toml,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def write_toml(tmp_path, body, name="project.toml"):
    path = tmp_path / name
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    return path


def run_cli(*args, cwd=None, env=None):
    """Invoke the CLI as a subprocess; returns CompletedProcess."""
    child_env = dict(os.environ)
    child_env["PYTHONPATH"] = REPO_ROOT + os.pathsep + child_env.get("PYTHONPATH", "")
    if env:
        child_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "bloomery", *args],
        capture_output=True, text=True, cwd=cwd, env=child_env,
    )


# ── TOML helpers ─────────────────────────────────────────────────

def test_parse_toml_returns_a_plain_dict(tmp_path):
    path = write_toml(tmp_path, """
        [variables]
        a = 1
    """)
    config = parse_toml(path)
    assert config["variables"]["a"] == 1


def test_load_variables_is_raw_unresolved(tmp_path):
    """A dispatch table stays a dict; resolution is the Evaluator's job."""
    path = write_toml(tmp_path, """
        [variables]
        compiler = "g++"
        ext = { on = "platform", windows = ".exe" }
    """)
    variables = load_variables(parse_toml(path))
    assert variables["compiler"] == "g++"
    assert variables["ext"] == {"on": "platform", "windows": ".exe"}


def test_load_variables_without_a_variables_table(tmp_path):
    path = write_toml(tmp_path, """
        [meta]
        name = "x"
    """)
    assert load_variables(parse_toml(path)) == {}


def test_load_profiles(tmp_path):
    path = write_toml(tmp_path, """
        [profiles.debug]
        debug = true

        [profiles.release]
        debug = false
    """)
    profiles = load_profiles(parse_toml(path))
    assert profiles["debug"]["debug"] is True
    assert profiles["release"]["debug"] is False


# ── DAG ───────────────────────────────────────────────────────────

def test_topo_sort_orders_dependencies_first(tmp_path):
    path = write_toml(tmp_path, """
        [tasks.build]
        command = "echo build"
        [tasks.run]
        depends = ["build"]
        command = "echo run"
    """)
    config = parse_toml(path)
    order = TaskDAG().build(config).topo_sort_with_config(config)
    assert order.index("build") < order.index("run")


def test_task_key_is_the_target_name_directly(tmp_path):
    """No alias indirection — the [tasks.X] key is the invocation name."""
    path = write_toml(tmp_path, """
        [tasks.build]
        command = "echo build"
    """)
    config = parse_toml(path)
    order = TaskDAG().build(config).topo_sort_with_config(config)
    assert order == ["build"]


def test_targets_pull_in_transitive_dependencies(tmp_path):
    path = write_toml(tmp_path, """
        [tasks.a]
        command = "echo a"
        [tasks.b]
        depends = ["a"]
        command = "echo b"
        [tasks.c]
        depends = ["b"]
        command = "echo c"
    """)
    config = parse_toml(path)
    order = TaskDAG().build(config).topo_sort_with_config(config, targets=["c"])
    assert order == ["a", "b", "c"]


def test_cycle_is_detected(tmp_path):
    path = write_toml(tmp_path, """
        [tasks.a]
        depends = ["b"]
        command = "echo a"
        [tasks.b]
        depends = ["a"]
        command = "echo b"
    """)
    config = parse_toml(path)
    dag = TaskDAG().build(config)
    with pytest.raises(CyclicDependencyError):
        dag.topo_sort_with_config(config)


def test_always_run_task_is_excluded_from_the_default_build(tmp_path):
    path = write_toml(tmp_path, """
        [tasks.build]
        command = "echo build"
        [tasks.clean]
        always_run = true
        default = false
        command = "echo clean"
    """)
    config = parse_toml(path)
    order = TaskDAG().build(config).topo_sort_with_config(config)
    assert "clean" not in order
    assert "build" in order


def test_ready_waves_group_independent_tasks(tmp_path):
    path = write_toml(tmp_path, """
        [tasks.a]
        command = "echo a"
        [tasks.b]
        command = "echo b"
        [tasks.join]
        depends = ["a", "b"]
        command = "echo j"
    """)
    config = parse_toml(path)
    dag = TaskDAG().build(config)
    order = dag.topo_sort_with_config(config)
    waves = dag.ready_waves(config, order)
    assert [set(w) for w in waves] == [{"a", "b"}, {"join"}]


# ── cache ─────────────────────────────────────────────────────────

def test_cache_hit_and_miss_on_content_change(tmp_path):
    src = tmp_path / "a.cpp"
    src.write_text("int main(){}")
    cache = BuildCache(str(tmp_path))

    h1 = cache.compute_task_hash("g++ a.cpp", [str(src)])
    cache.update("build", h1, outputs=[])
    assert cache.is_cached("build", h1, str(tmp_path))

    src.write_text("int main(){return 1;}")
    h2 = cache.compute_task_hash("g++ a.cpp", [str(src)])
    assert h2 != h1
    assert not cache.is_cached("build", h2, str(tmp_path))


def test_cache_misses_when_a_recorded_output_is_missing(tmp_path):
    cache = BuildCache(str(tmp_path))
    h = cache.compute_task_hash("g++ a.cpp", [])
    cache.update("build", h, outputs=["main.exe"])
    assert not cache.is_cached("build", h, str(tmp_path))

    (tmp_path / "main.exe").write_text("binary")
    assert cache.is_cached("build", h, str(tmp_path))


def test_corrupt_cache_file_is_ignored(tmp_path):
    (tmp_path / ".bloomery_cache.json").write_text("{not json")
    assert BuildCache(str(tmp_path)).data == {}


def test_invalidate_clears_everything(tmp_path):
    cache = BuildCache(str(tmp_path))
    cache.update("build", "abc")
    cache.invalidate()
    assert cache.data == {}


# ── CLI end-to-end ────────────────────────────────────────────────

MINIMAL = """
    [meta]
    name = "demo"
    [tasks.hello]
    command = "echo hello"
"""


def test_cli_dry_run(tmp_path):
    write_toml(tmp_path, MINIMAL)
    r = run_cli(str(tmp_path / "project.toml"), "--dry-run")
    assert r.returncode == 0
    assert "echo hello" in r.stdout


def test_cli_unknown_target_fails_loudly(tmp_path):
    write_toml(tmp_path, MINIMAL)
    r = run_cli(str(tmp_path / "project.toml"), "nosuchtarget", "--dry-run")
    assert r.returncode == 1
    assert "Unknown target" in r.stderr
    assert "Traceback" not in r.stderr


def test_cli_target_by_task_key(tmp_path):
    write_toml(tmp_path, MINIMAL)
    r = run_cli(str(tmp_path / "project.toml"), "hello", "--dry-run")
    assert r.returncode == 0


def test_cli_missing_file_has_no_traceback(tmp_path):
    r = run_cli(str(tmp_path / "absent.toml"))
    assert r.returncode == 1
    assert "Missing file" in r.stderr
    assert "Traceback" not in r.stderr


def test_cli_malformed_toml_has_no_traceback(tmp_path):
    (tmp_path / "bad.toml").write_text("this = is not [ valid\n")
    r = run_cli(str(tmp_path / "bad.toml"))
    assert r.returncode == 1
    assert "Could not parse" in r.stderr
    assert "Traceback" not in r.stderr


def test_cli_cycle_has_no_traceback(tmp_path):
    write_toml(tmp_path, """
        [tasks.a]
        depends = ["b"]
        command = "echo a"
        [tasks.b]
        depends = ["a"]
        command = "echo b"
    """)
    r = run_cli(str(tmp_path / "project.toml"), "--dry-run")
    assert r.returncode == 1
    assert "Cycle detected" in r.stderr


def test_cli_version():
    r = run_cli("--version")
    assert r.returncode == 0
    assert "bloomery" in r.stdout.lower()


def test_cli_list_targets(tmp_path):
    write_toml(tmp_path, MINIMAL)
    r = run_cli(str(tmp_path / "project.toml"), "--list")
    assert r.returncode == 0
    assert "hello" in r.stdout


def test_cli_failing_task_exits_nonzero(tmp_path):
    write_toml(tmp_path, """
        [meta]
        name = "fail"
        [tasks.boom]
        command = 'python -c "import sys; sys.exit(3)"'
    """)
    r = run_cli(str(tmp_path / "project.toml"))
    assert r.returncode == 1
    assert "failed" in r.stderr.lower()
    assert "Traceback" not in r.stderr


def test_dry_run_does_not_write_a_cache_file(tmp_path):
    write_toml(tmp_path, MINIMAL)
    run_cli(str(tmp_path / "project.toml"), "--dry-run")
    assert not (tmp_path / ".bloomery_cache.json").exists()


def test_profile_overrides_variables(tmp_path):
    write_toml(tmp_path, """
        [meta]
        name = "prof"
        [variables]
        opt = "-O0"
        [profiles.release]
        opt = "-O2"
        [tasks.build]
        command = "g++ {opt}"
    """)
    base = run_cli(str(tmp_path / "project.toml"), "--dry-run")
    rel = run_cli(str(tmp_path / "project.toml"), "--dry-run", "--profile", "release")
    assert "g++ -O0" in base.stdout
    assert "g++ -O2" in rel.stdout


def test_cli_define_overrides_a_variable(tmp_path):
    write_toml(tmp_path, """
        [meta]
        name = "d"
        [variables]
        opt = "-O0"
        [tasks.build]
        command = "g++ {opt}"
    """)
    r = run_cli(str(tmp_path / "project.toml"), "--dry-run", "-D", "opt=-O3")
    assert "g++ -O3" in r.stdout


def test_cli_define_feeds_a_dispatch_table(tmp_path):
    write_toml(tmp_path, """
        [meta]
        name = "d"
        [variables]
        debug = true
        [tasks.build]
        command = "echo"
        flags = { on = "debug", true = "-g", false = "-O2" }
    """)
    r = run_cli(str(tmp_path / "project.toml"), "--dry-run", "-D", "debug=false")
    assert "-O2" in r.stdout
    assert "-g" not in r.stdout


# ── shipped example ───────────────────────────────────────────────

EXAMPLE = os.path.join(REPO_ROOT, "examples", "hello-cpp", "project.toml")


def test_shipped_example_resolves(tmp_path):
    r = run_cli(EXAMPLE, "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "Traceback" not in r.stderr


def test_shipped_example_lists_its_targets():
    r = run_cli(EXAMPLE, "--list")
    assert r.returncode == 0
    for name in ("build", "run", "clean"):
        assert name in r.stdout
