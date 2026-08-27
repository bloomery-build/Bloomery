"""Tests for the task graph, build cache, INI helpers, and the CLI."""

import io
import os
import subprocess
import sys
import textwrap

import pytest

from bloomery import (
    BuildCache,
    CyclicDependencyError,
    TaskDAG,
    UnknownTargetError,
    load_profiles,
    load_variables,
    parse_ini,
    task_aliases,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def write_ini(tmp_path, body, name="project.ini"):
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


# ── INI helpers ───────────────────────────────────────────────────

def test_duplicate_sections_merge(tmp_path):
    """The shipped example splits [Variables]; that must not be an error."""
    path = write_ini(tmp_path, """
        [Variables]
        a = 1

        ; Computed
        [Variables]
        b = 2
    """)
    config = parse_ini(path)
    assert load_variables(config) == {"a": "1", "b": "2"}


def test_parse_ini_preserves_key_case(tmp_path):
    path = write_ini(tmp_path, """
        [Tasks.Build]
        Command = g++
    """)
    assert parse_ini(path).get("Tasks.Build", "Command") == "g++"


def test_task_aliases_without_a_tasks_section(tmp_path):
    """Regression: this used to raise NoOptionError via config.get('Tasks', {})."""
    path = write_ini(tmp_path, """
        [Meta]
        Name = x
    """)
    assert task_aliases(parse_ini(path)) == {}


def test_task_aliases_maps_alias_to_section(tmp_path):
    path = write_ini(tmp_path, """
        [Tasks]
        build = Build
    """)
    assert task_aliases(parse_ini(path)) == {"build": "Build"}


def test_load_profiles(tmp_path):
    path = write_ini(tmp_path, """
        [Profiles.debug]
        debug = true

        [Profiles.release]
        debug = false
    """)
    profiles = load_profiles(parse_ini(path))
    assert profiles["debug"]["debug"] == "true"
    assert profiles["release"]["debug"] == "false"


# ── DAG ───────────────────────────────────────────────────────────

def test_topo_sort_orders_dependencies_first(tmp_path):
    path = write_ini(tmp_path, """
        [Variables]
        output = x
        [Tasks]
        build = Build
        run = Run
        [Tasks.Build]
        Command = echo build
        [Tasks.Run]
        Depends = Build
        Command = echo run
    """)
    config = parse_ini(path)
    order = TaskDAG().build(config).topo_sort_with_config(config)
    assert order.index("Build") < order.index("Run")


def test_targets_pull_in_transitive_dependencies(tmp_path):
    path = write_ini(tmp_path, """
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
    config = parse_ini(path)
    order = TaskDAG().build(config).topo_sort_with_config(config, targets=["C"])
    assert order == ["A", "B", "C"]


def test_cycle_is_detected(tmp_path):
    path = write_ini(tmp_path, """
        [Tasks]
        a = A
        b = B
        [Tasks.A]
        Depends = B
        Command = echo a
        [Tasks.B]
        Depends = A
        Command = echo b
    """)
    config = parse_ini(path)
    dag = TaskDAG().build(config)
    with pytest.raises(CyclicDependencyError):
        dag.topo_sort_with_config(config)


def test_always_run_task_is_excluded_from_the_default_build(tmp_path):
    path = write_ini(tmp_path, """
        [Tasks]
        build = Build
        clean = Clean
        [Tasks.Build]
        Command = echo build
        [Tasks.Clean]
        AlwaysRun = yes
        Default = no
        Command = echo clean
    """)
    config = parse_ini(path)
    order = TaskDAG().build(config).topo_sort_with_config(config)
    assert "Clean" not in order
    assert "Build" in order


# ── cache ─────────────────────────────────────────────────────────

def test_cache_hit_and_miss_on_content_change(tmp_path):
    src = tmp_path / "a.cpp"
    src.write_text("int main(){}")
    cache = BuildCache(str(tmp_path))

    h1 = cache.compute_task_hash("g++ a.cpp", [str(src)])
    cache.update("Build", h1, outputs=[])
    assert cache.is_cached("Build", h1, str(tmp_path))

    src.write_text("int main(){return 1;}")
    h2 = cache.compute_task_hash("g++ a.cpp", [str(src)])
    assert h2 != h1
    assert not cache.is_cached("Build", h2, str(tmp_path))


def test_cache_misses_when_the_command_changes(tmp_path):
    cache = BuildCache(str(tmp_path))
    h1 = cache.compute_task_hash("g++ -O0 a.cpp", [])
    h2 = cache.compute_task_hash("g++ -O2 a.cpp", [])
    assert h1 != h2


def test_cache_misses_when_a_recorded_output_is_missing(tmp_path):
    """The 'notes' bug: unchanged input but a deleted binary must rebuild."""
    cache = BuildCache(str(tmp_path))
    h = cache.compute_task_hash("g++ a.cpp", [])
    cache.update("Build", h, outputs=["main.exe"])
    assert not cache.is_cached("Build", h, str(tmp_path))

    (tmp_path / "main.exe").write_text("binary")
    assert cache.is_cached("Build", h, str(tmp_path))


def test_cache_roundtrips_through_disk(tmp_path):
    cache = BuildCache(str(tmp_path))
    cache.update("Build", "abc", outputs=["main.exe"])
    cache.save()
    assert BuildCache(str(tmp_path)).data["Build"]["hash"] == "abc"


def test_corrupt_cache_file_is_ignored(tmp_path):
    (tmp_path / ".bloomery_cache.json").write_text("{not json")
    assert BuildCache(str(tmp_path)).data == {}


def test_invalidate_clears_everything(tmp_path):
    cache = BuildCache(str(tmp_path))
    cache.update("Build", "abc")
    cache.invalidate()
    assert cache.data == {}


# ── CLI end-to-end ────────────────────────────────────────────────

MINIMAL = """
    [Meta]
    Name = demo
    [Tasks]
    hello = Hello
    [Tasks.Hello]
    Command = echo hello
"""


def test_cli_dry_run(tmp_path):
    write_ini(tmp_path, MINIMAL)
    r = run_cli(str(tmp_path / "project.ini"), "--dry-run")
    assert r.returncode == 0
    assert "echo hello" in r.stdout


def test_cli_runs_without_an_output_variable(tmp_path):
    """Regression: config.get('Tasks', {}) crashed when 'output' was unset."""
    write_ini(tmp_path, MINIMAL)
    r = run_cli(str(tmp_path / "project.ini"), "--dry-run")
    assert r.returncode == 0
    assert "Traceback" not in r.stderr


def test_cli_unknown_target_fails_loudly(tmp_path):
    write_ini(tmp_path, MINIMAL)
    r = run_cli(str(tmp_path / "project.ini"), "nosuchtarget", "--dry-run")
    assert r.returncode == 1
    assert "Unknown target" in r.stderr
    assert "Traceback" not in r.stderr


def test_cli_accepts_both_alias_and_section_name(tmp_path):
    write_ini(tmp_path, MINIMAL)
    for target in ("hello", "Hello"):
        r = run_cli(str(tmp_path / "project.ini"), target, "--dry-run")
        assert r.returncode == 0, target


def test_cli_missing_file_has_no_traceback(tmp_path):
    r = run_cli(str(tmp_path / "absent.ini"))
    assert r.returncode == 1
    assert "Missing file" in r.stderr
    assert "Traceback" not in r.stderr


def test_cli_malformed_ini_has_no_traceback(tmp_path):
    (tmp_path / "bad.ini").write_text("not an ini file\n[Meta\n")
    r = run_cli(str(tmp_path / "bad.ini"))
    assert r.returncode == 1
    assert "Could not parse" in r.stderr
    assert "Traceback" not in r.stderr


def test_cli_cycle_has_no_traceback(tmp_path):
    write_ini(tmp_path, """
        [Tasks]
        a = A
        b = B
        [Tasks.A]
        Depends = B
        Command = echo a
        [Tasks.B]
        Depends = A
        Command = echo b
    """)
    r = run_cli(str(tmp_path / "project.ini"), "--dry-run")
    assert r.returncode == 1
    assert "Cycle detected" in r.stderr
    assert "Traceback" not in r.stderr


def test_cli_version():
    r = run_cli("--version")
    assert r.returncode == 0
    assert "bloomery" in r.stdout.lower()


def test_cli_list_targets(tmp_path):
    write_ini(tmp_path, MINIMAL)
    r = run_cli(str(tmp_path / "project.ini"), "--list")
    assert r.returncode == 0
    assert "hello" in r.stdout


def test_cli_failing_task_exits_nonzero(tmp_path):
    write_ini(tmp_path, """
        [Meta]
        Name = fail
        [Tasks]
        boom = Boom
        [Tasks.Boom]
        Command = python -c "import sys; sys.exit(3)"
    """)
    r = run_cli(str(tmp_path / "project.ini"))
    assert r.returncode == 1
    assert "failed" in r.stderr.lower()
    assert "Traceback" not in r.stderr


def test_dry_run_does_not_write_a_cache_file(tmp_path):
    write_ini(tmp_path, MINIMAL)
    run_cli(str(tmp_path / "project.ini"), "--dry-run")
    assert not (tmp_path / ".bloomery_cache.json").exists()


def test_profile_overrides_variables(tmp_path):
    write_ini(tmp_path, """
        [Meta]
        Name = prof
        [Variables]
        opt = -O0
        [Profiles.release]
        opt = -O2
        [Tasks]
        build = Build
        [Tasks.Build]
        Command = g++ {opt}
    """)
    base = run_cli(str(tmp_path / "project.ini"), "--dry-run")
    rel = run_cli(str(tmp_path / "project.ini"), "--dry-run", "--profile", "release")
    assert "g++ -O0" in base.stdout
    assert "g++ -O2" in rel.stdout


def test_cli_define_overrides_a_variable(tmp_path):
    write_ini(tmp_path, """
        [Meta]
        Name = d
        [Variables]
        opt = -O0
        [Tasks]
        build = Build
        [Tasks.Build]
        Command = g++ {opt}
    """)
    r = run_cli(str(tmp_path / "project.ini"), "--dry-run", "-D", "opt=-O3")
    assert "g++ -O3" in r.stdout


# ── inline comments ───────────────────────────────────────────────

def test_inline_comment_is_stripped_from_raw_fields(tmp_path):
    """Meta.System never reaches the directive engine, so the parser
    must strip 'value ; note' itself — otherwise the mold lookup uses
    the comment text as part of the name."""
    config = parse_ini(write_ini(tmp_path, """
        [Meta]
        System = c++              ; which mold to load
    """))
    assert config.get("Meta", "System") == "c++"


def test_inline_comment_is_stripped_from_variables(tmp_path):
    config = parse_ini(write_ini(tmp_path, """
        [Variables]
        compiler = g++            ; project-level variable
    """))
    assert load_variables(config) == {"compiler": "g++"}


def test_semicolon_inside_a_directive_survives(tmp_path):
    """'<files in (lib; .cpp)>' must not be truncated at its semicolon."""
    config = parse_ini(write_ini(tmp_path, """
        [Tasks.Build]
        Files = <files in (lib; .cpp|.cc)>
    """))
    assert config.get("Tasks.Build", "Files") == "<files in (lib; .cpp|.cc)>"


# ── shipped example ───────────────────────────────────────────────

EXAMPLE = os.path.join(REPO_ROOT, "examples", "hello-cpp", "project.ini")


def test_shipped_example_resolves(tmp_path):
    """The example in the README must at least plan a build cleanly."""
    r = run_cli(EXAMPLE, "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "Traceback" not in r.stderr


def test_shipped_example_lists_its_targets():
    r = run_cli(EXAMPLE, "--list")
    assert r.returncode == 0
    for alias in ("build", "run", "clean"):
        assert alias in r.stdout


def test_shipped_example_hook_uses_a_real_event():
    """Hooks fire as Pre<Task>/Post<Task>/OnFail<Task>; anything else
    is silently ignored, so the example must not advertise a dead one."""
    config = parse_ini(EXAMPLE)
    tasks = set(task_aliases(config).values())
    for key, _value in config.items("Plugins.Hooks"):
        assert any(key == f"{p}{t}" for p in ("Pre", "Post", "OnFail") for t in tasks), \
            f"hook {key!r} matches no Pre/Post/OnFail<Task> event"


# ── self-management ───────────────────────────────────────────────

def test_repo_root_finds_this_checkout():
    from bloomery import core
    assert core.repo_root() == REPO_ROOT


def test_self_commands_dispatch_without_touching_argparse(monkeypatch):
    """'bloomery install' must not be parsed as a project path."""
    from bloomery import core
    ran = []
    monkeypatch.setattr(core, "_run", lambda *cmd: ran.append(cmd))
    for name in ("install", "update", "uninstall"):
        ran.clear()
        monkeypatch.setattr(sys, "argv", ["bloomery", name])
        assert core.cli() == 0
        assert ran, f"{name} ran no command"


def test_update_pulls_a_dev_checkout(monkeypatch):
    from bloomery import core
    ran = []
    monkeypatch.setattr(core, "repo_root", lambda: "/some/checkout")
    monkeypatch.setattr(core, "_run", lambda *cmd: ran.append(cmd))
    monkeypatch.setattr(sys, "argv", ["bloomery", "update"])
    assert core.cli() == 0
    assert ran[0][:2] == ("git", "-C")


def test_update_falls_back_to_pip_upgrade_without_a_checkout(monkeypatch):
    """A plain 'pip install bloomery-build' isn't a git checkout — update
    should upgrade the package instead of telling the user to switch to
    dev mode."""
    from bloomery import core
    ran = []
    monkeypatch.setattr(core, "repo_root", lambda: None)
    monkeypatch.setattr(core, "_run", lambda *cmd: ran.append(cmd))
    monkeypatch.setattr(sys, "argv", ["bloomery", "update"])
    assert core.cli() == 0
    assert "pip" in ran[0]
    assert "--upgrade" in ran[0]
    assert "bloomery-build" in ran[0]


def test_a_project_named_install_still_builds(tmp_path):
    """The intercept is exact-match on a lone argv[1]; a path still wins."""
    write_ini(tmp_path, MINIMAL, name="install.ini")
    r = run_cli(str(tmp_path / "install.ini"), "--dry-run")
    assert r.returncode == 0
