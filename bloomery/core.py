"""
Bloomery — a TOML-native build system.

Every project/mold file is plain TOML. Two conventions carry all the
"metaprogramming":

Interpolation (in any string value):
    {name}              Variable / dispatch-table reference
    {env.NAME}          Environment variable
    {mold.Field}        Mold definition lookup
    {task.T.Field}      Another task's field
    {task.T.outputs}    Files another task produced

Reserved-key tables (a TOML inline table with one of these keys):
    { on = "var", <value> = ..., default = ... }   Dispatch on a variable's
                                                     current value
    { ending = [".ext", ...] }                      Files by extension
    { in = "dir", ending = [".ext", ...] }           ...scoped to a directory
    { matching = "glob" }                            Files by glob pattern
    { shell = "cmd" }                                Captured stdout of cmd
    { exists = "path" }                              "true" / "false"
    { prefix = "-D", items = [...] }                 Prefix each item, joined

A dispatch value's own "true"/"false"/"windows"/... branches, and every
reserved-key argument, are themselves resolved recursively — a branch can
be another dispatch table, a list, or a plain string.

Task fields:
    mode = "per-file"   One command per input file, cached individually
    headers = [...]     Hashed but kept off the command line (#include deps)
    depfile = "..."     Make-format depfile to read #include deps from
    depends = ["..."]   Task names this one depends on
    always_run = true   Skip the cache, always execute
    default = false     Excluded from the default (no-target) build

Usage:
    bloomery <project.toml> [targets...] [options]
    python -m bloomery <project.toml> [targets...] [options]

Options:
    --clean          Force full rebuild (ignore cache)
    --dry-run        Show commands without executing
    --list           List available targets and exit
    --verbose        Show resolution details
    -D VAR=VALUE     Define/override a variable
    --profile NAME   Activate a profile (overlays [profiles.NAME])
    -j, --jobs N     Run independent tasks in parallel (0 = one per CPU)
    --keep-going     Don't cancel sibling tasks after a failure
    --version        Print version and exit

Self-management:
    bloomery install     pip install -e the git checkout (dev mode)
    bloomery update      git pull a dev checkout, else pip upgrade
    bloomery uninstall   pip uninstall bloomery-build
"""

__version__ = "0.2.0"

try:
    import tomllib
except ModuleNotFoundError:                # Python < 3.11
    import tomli as tomllib

import subprocess
import glob
import re
import os
import sys
import hashlib
import json
import argparse
import fnmatch
import threading
import platform as platform_module
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor


# CONST
CACHE_FILE = ".bloomery_cache.json"


# EXCEPT
class BloomeryError(Exception):
    """Base exception for Bloomery build errors."""


class CyclicDependencyError(BloomeryError):
    """Raised when a cycle is detected in the task dependency graph."""


class TaskFailedError(BloomeryError):
    """Raised when a task command exits with a non-zero code."""


class ConfigNotFoundError(BloomeryError):
    """Raised when a project or mold TOML file cannot be found."""


class ConfigParseError(BloomeryError):
    """Raised when a TOML file is malformed."""


class UnknownTargetError(BloomeryError):
    """Raised when a requested target matches no task."""


class MoldNotFoundError(ConfigNotFoundError):
    """Raised when a mold cannot be located on the search path."""


# CONTEXT
class Context:
    """variables holds raw TOML values, resolved lazily via Evaluator"""

    def __init__(self, project_dir=".", variables=None, mold_config=None,
                 project_config=None, verbose=False, cli_vars=None):
        self.project_dir = project_dir
        self.variables = dict(variables or {})
        self.mold_config = mold_config
        self.project_config = project_config
        self.platform = self._detect_platform()
        self.variables.setdefault("platform", self.platform)
        self.verbose = verbose
        self.resolved_files = {}     # task_name -> [file_paths]
        self.resolved_outputs = {}   # task_name -> [output_paths]
        self.current_task = None

        # CLI overrides take highest precedence
        if cli_vars:
            self.variables.update(cli_vars)

    def fork(self):
        """Copy for one task to mutate; resolved_files/outputs stay shared"""
        clone = Context.__new__(Context)
        clone.__dict__.update(self.__dict__)
        clone.variables = dict(self.variables)
        clone.current_task = None
        return clone

    @staticmethod
    def _detect_platform():
        s = platform_module.system().lower()
        if s == "windows":
            return "windows"
        if s == "darwin":
            return "macos"
        if s == "linux":
            return "linux"
        return s

    def get_var(self, name, default=""):
        return self.variables.get(name, default)

    def set_var(self, name, value):
        self.variables[name] = value


# EVAL
class Evaluator:
    """Resolves a TOML value: dispatch tables, reserved keys, {interp}"""

    _INTERP_RE = re.compile(r'\{([^}]+)\}')

    def __init__(self, ctx: Context):
        self.ctx = ctx
        self._resolving = []   # names currently being resolved, cycle guard

    def resolve_str(self, value):
        """Resolve to a single string (command/flags/output fields)"""
        result = self._resolve(value)
        if isinstance(result, list):
            return " ".join(result)
        return str(result)

    def resolve_list(self, value):
        """Resolve to a list of strings (files/headers/exclude fields)"""
        result = self._resolve(value)
        if isinstance(result, list):
            return [str(x) for x in result]
        return [x for x in str(result).split() if x]

    def _resolve(self, value):
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, str):
            return self._interpolate(value)
        if isinstance(value, dict):
            return self._resolve_table(value)
        if isinstance(value, list):
            return [self._resolve(v) for v in value]
        if value is None:
            return ""
        return value   # int / float pass through as-is

    def _resolve_table(self, table):
        if "on" in table:
            return self._resolve_dispatch(table)
        if "in" in table and "ending" in table:
            return self._files_ending(table["ending"], table["in"])
        if "ending" in table:
            return self._files_ending(table["ending"])
        if "matching" in table:
            return self._files_matching(table["matching"])
        if "shell" in table:
            return self._shell(self.resolve_str(table["shell"]))
        if "exists" in table:
            path = self.resolve_str(table["exists"])
            full = os.path.join(self.ctx.project_dir, path)
            return "true" if os.path.exists(full) else "false"
        if "prefix" in table:
            prefix = self.resolve_str(table["prefix"])
            items = self.resolve_list(table.get("items", []))
            return [f"{prefix}{item}" for item in items]
        raise BloomeryError(f"Unrecognized table: {sorted(table.keys())}")

    def _resolve_dispatch(self, table):
        var_name = table["on"]
        key = self._stringify_key(self.ctx.get_var(var_name))
        if key in table:
            return self._resolve(table[key])
        if "default" in table:
            return self._resolve(table["default"])
        return ""

    @staticmethod
    def _stringify_key(value):
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    def _interpolate(self, text):
        def repl(m):
            expr = m.group(1).strip()

            if expr.startswith("env."):
                return os.environ.get(expr[4:], "")
            if expr.startswith("mold."):
                return self._mold_field(expr[5:])
            if expr.startswith("task."):
                parts = expr[5:].split(".", 1)
                return self._task_field(*parts) if len(parts) == 2 else ""

            if expr in self._resolving:
                raise BloomeryError(
                    "Cyclic variable reference: "
                    + " -> ".join(self._resolving + [expr])
                )
            raw = self.ctx.variables.get(expr)
            if raw is None:
                return ""
            self._resolving.append(expr)
            try:
                return self.resolve_str(raw)
            finally:
                self._resolving.pop()

        return self._INTERP_RE.sub(repl, text)

    def _mold_field(self, field_name):
        if self.ctx.mold_config is None:
            return ""
        defs = self.ctx.mold_config.get("definitions", {})
        if field_name not in defs:
            return ""
        return self.resolve_str(defs[field_name])

    def _task_field(self, task_name, field_name):
        if field_name == "outputs":
            return " ".join(self.ctx.resolved_outputs.get(task_name, []))
        if self.ctx.project_config is None:
            return ""
        task = self.ctx.project_config.get("tasks", {}).get(task_name, {})
        if field_name not in task:
            return ""
        return self.resolve_str(task[field_name])

    def _files_ending(self, exts, directory=""):
        exts = [self.resolve_str(e) for e in exts]
        directory = self.resolve_str(directory) if directory else ""
        files = []
        for ext in exts:
            pattern = os.path.join(self.ctx.project_dir, directory, f"*{ext}")
            files.extend(glob.glob(pattern))
        return sorted(set(
            os.path.relpath(f, self.ctx.project_dir) for f in files
        ))

    def _files_matching(self, pattern):
        pattern = self.resolve_str(pattern)
        full = os.path.join(self.ctx.project_dir, pattern)
        files = glob.glob(full, recursive=True)
        return sorted(set(
            os.path.relpath(f, self.ctx.project_dir) for f in files
        ))

    def _shell(self, cmd):
        try:
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True,
                timeout=30, cwd=self.ctx.project_dir,
            )
            return result.stdout.strip()
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            if self.ctx.verbose:
                print(f"  [WARN] shell failed: {cmd!r}: {e}")
            return ""


# DEPFILE
def parse_depfile(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except (FileNotFoundError, OSError):
        return []

    text = text.replace("\\\n", " ").replace("\\\r\n", " ")

    deps = []
    for line in text.splitlines():
        if ":" not in line:
            continue
        _target, _, prereqs = line.partition(":")
        for dep in prereqs.split():
            if dep and dep != "\\" and dep not in deps:
                deps.append(dep)
    return deps


# CACHE
class BuildCache:
    """Content-hash cache; a hit also requires every output to still exist"""

    def __init__(self, project_dir):
        self.path = os.path.join(project_dir, CACHE_FILE)
        self.data = self._load()

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2)

    @staticmethod
    def _file_hash(filepath):
        h = hashlib.sha256()
        try:
            with open(filepath, "rb") as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    h.update(chunk)
            return h.hexdigest()
        except (FileNotFoundError, OSError):
            return ""

    def compute_task_hash(self, command_str, files):
        h = hashlib.sha256()
        h.update(command_str.encode())
        for filepath in sorted(files):
            h.update(filepath.encode())
            h.update(self._file_hash(filepath).encode())
        return h.hexdigest()

    def is_cached(self, task_name, task_hash, project_dir=""):
        entry = self.data.get(task_name)
        if entry is None:
            return False
        if isinstance(entry, str):
            return entry == task_hash
        if entry.get("hash", "") != task_hash:
            return False
        for out_path in entry.get("outputs", []):
            full = os.path.join(project_dir, out_path) if project_dir else out_path
            if not os.path.exists(full):
                return False
        return True

    def get_deps(self, task_name):
        """Header deps recorded by the previous build"""
        entry = self.data.get(task_name)
        if isinstance(entry, dict):
            return list(entry.get("deps", []))
        return []

    def update(self, task_name, task_hash, outputs=None, deps=None):
        self.data[task_name] = {
            "hash": task_hash,
            "outputs": outputs or [],
            "deps": deps or [],
        }

    def invalidate(self, task_name=None):
        if task_name:
            self.data.pop(task_name, None)
        else:
            self.data.clear()


# DAG
# task names are the [tasks.*] keys directly, no alias table
class TaskDAG:
    def __init__(self):
        self.graph = defaultdict(list)     # task -> [dependents]
        self.indegree = defaultdict(int)
        self.tasks = []

    def build(self, config):
        for name, task in config.get("tasks", {}).items():
            if name not in self.tasks:
                self.tasks.append(name)
            for dep in task.get("depends", []):
                self.graph[dep].append(name)
                self.indegree[name] = self.indegree.get(name, 0) + 1
            if name not in self.indegree:
                self.indegree[name] = 0
        return self

    def get_dependencies(self, task_name, config):
        return list(config.get("tasks", {}).get(task_name, {}).get("depends", []))

    def topo_sort_with_config(self, config, targets=None):
        """Topo sort. targets pulls in transitive deps, else the default set"""
        needed = self._collect_deps(targets, config) if targets else self._default_tasks(config)

        indeg = defaultdict(int)
        for t in needed:
            for dep in self.get_dependencies(t, config):
                if dep in needed:
                    indeg[t] += 1
            if t not in indeg:
                indeg[t] = 0

        q = deque(t for t in needed if indeg.get(t, 0) == 0)
        order = []
        while q:
            cur = q.popleft()
            order.append(cur)
            for nxt in self.graph.get(cur, []):
                if nxt in needed:
                    indeg[nxt] -= 1
                    if indeg[nxt] == 0:
                        q.append(nxt)

        if len(order) != len(needed):
            raise CyclicDependencyError("Cycle detected in task dependencies")
        return order

    def _default_tasks(self, config):
        """Tasks to run when no targets were given"""
        tasks = config.get("tasks", {})
        depended_on = set()
        for t in self.tasks:
            depended_on.update(self.get_dependencies(t, config))

        needed = set(depended_on)
        for t in self.tasks:
            deps = self.get_dependencies(t, config)
            if not deps:
                task = tasks.get(t, {})
                is_default = bool(task.get("default", True))
                always_run = bool(task.get("always_run", False))
                if is_default and not always_run:
                    needed.add(t)
            else:
                needed.add(t)
        return needed

    def ready_waves(self, config, order):
        """Group order into waves that can run concurrently"""
        # ponytail: joins each wave whole instead of a rolling ready queue.
        # Fine for shallow graphs; swap if a wide one stalls on a straggler.
        in_order = set(order)
        depth = {}
        for task in order:                      # already topological
            deps = [d for d in self.get_dependencies(task, config) if d in in_order]
            depth[task] = 1 + max((depth[d] for d in deps), default=-1)
        waves = defaultdict(list)
        for task in order:
            waves[depth[task]].append(task)
        return [waves[d] for d in sorted(waves)]

    def _collect_deps(self, roots, config):
        needed = set()
        stack = list(roots)
        while stack:
            t = stack.pop()
            if t in needed:
                continue
            needed.add(t)
            stack.extend(self.get_dependencies(t, config))
        return needed


# PLUGINS
# [plugins.hooks.<task>] holds pre/post/on_fail, scoped by nesting
# instead of a string-matched Pre<Task>/Post<Task> key
class PluginManager:
    _EVENT_PREFIX = {"pre": "Pre", "post": "Post", "on_fail": "OnFail"}

    def __init__(self, config, evaluator):
        self.config = config
        self.evaluator = evaluator
        self._prefix = None
        self._hooks = defaultdict(list)

    def load(self):
        command = self.config.get("plugins", {}).get("command", {})
        if "prefix" in command:
            self._prefix = self.evaluator.resolve_str(command["prefix"])

        hooks = self.config.get("plugins", {}).get("hooks", {})
        for task_name, task_hooks in hooks.items():
            for kind, cmd in task_hooks.items():
                prefix = self._EVENT_PREFIX.get(kind, kind)
                self._hooks[f"{prefix}{task_name}"].append(
                    self.evaluator.resolve_str(cmd))

    @property
    def command_prefix(self):
        return self._prefix or ""

    def get_hooks(self, event):
        return self._hooks.get(event, [])

    def run_hooks(self, event, project_dir):
        for cmd in self.get_hooks(event):
            if self.evaluator.ctx.verbose:
                print(f"  [HOOK:{event}] {cmd}")
            subprocess.run(cmd, shell=True, cwd=project_dir)


# RUNNER
class TaskRunner:
    """Resolve fields, check cache, execute. mode=per-file: one command+cache entry per file"""

    def __init__(self, evaluator, cache, plugins, dry_run=False):
        self.evaluator = evaluator
        self.cache = cache
        self.plugins = plugins
        self.dry_run = dry_run
        self._cache_lock = threading.Lock()
        self._print_lock = threading.Lock()

    def _emit(self, lines):
        """Print one task's lines atomically"""
        with self._print_lock:
            for line in lines:
                print(line)
            sys.stdout.flush()

    def run_task(self, name, task_config, project_dir, evaluator=None):
        """Run one task, return its output lines"""
        evaluator = evaluator or self.evaluator
        ctx = evaluator.ctx
        ctx.current_task = name

        always_run = bool(task_config.get("always_run", False))
        per_file = task_config.get("mode", "") == "per-file"

        files = evaluator.resolve_list(task_config.get("files", []))

        patterns = evaluator.resolve_list(task_config.get("exclude", []))
        if patterns:
            files = [f for f in files
                     if not any(fnmatch.fnmatch(os.path.basename(f), p)
                                for p in patterns)]

        # hashed but never passed to the compiler: #include inputs
        headers = evaluator.resolve_list(task_config.get("headers", []))

        if files:
            stem = os.path.splitext(os.path.basename(files[0]))[0]
            ctx.set_var("input", stem)

        ctx.resolved_files[name] = files

        if per_file:
            return self._run_per_file(
                name, task_config, project_dir, evaluator, files, headers, always_run)
        return self._run_whole(
            name, task_config, project_dir, evaluator, files, headers, always_run)

    def _run_whole(self, name, task_config, project_dir, evaluator,
                   files, headers, always_run):
        ctx = evaluator.ctx

        cmd_str = evaluator.resolve_str(task_config.get("command", ""))
        flags_str = evaluator.resolve_str(task_config.get("flags", ""))
        output_str = evaluator.resolve_str(task_config.get("output", ""))

        output_name = task_config.get("output_name")
        if output_name:
            ctx.set_var("output", evaluator.resolve_str(output_name))

        command_line = self._assemble(cmd_str, flags_str, files, output_str)
        output_files = self._extract_output_paths(output_str, project_dir)
        ctx.resolved_outputs[name] = output_files

        depfile = evaluator.resolve_str(task_config.get("depfile", "")).strip()

        return self._run_one(
            key=name, label=name, command_line=command_line,
            sources=files + headers, outputs=output_files,
            project_dir=project_dir, depfile=depfile, always_run=always_run,
        )

    def _run_per_file(self, name, task_config, project_dir, evaluator,
                      files, headers, always_run):
        ctx = evaluator.ctx
        lines = []
        all_outputs = []
        ran_any = False

        for path in files:
            stem = os.path.splitext(os.path.basename(path))[0]
            ctx.set_var("stem", stem)
            ctx.set_var("input", stem)

            cmd_str = evaluator.resolve_str(task_config.get("command", ""))
            flags_str = evaluator.resolve_str(task_config.get("flags", ""))
            output_str = evaluator.resolve_str(task_config.get("output", ""))

            command_line = self._assemble(cmd_str, flags_str, [path], output_str)
            outputs = self._extract_output_paths(output_str, project_dir)
            all_outputs.extend(outputs)

            depfile = evaluator.resolve_str(task_config.get("depfile", "")).strip()
            self._ensure_output_dirs(outputs, depfile, project_dir)

            sub = self._run_one(
                key=f"{name}:{path}", label=f"{name} [{path}]",
                command_line=command_line, sources=[path] + headers,
                outputs=outputs, project_dir=project_dir,
                depfile=depfile, always_run=always_run,
            )
            lines.extend(sub)
            ran_any = ran_any or not sub[0].startswith("[SKIP]")

        ctx.resolved_outputs[name] = all_outputs

        # per-file lines were emitted as they ran; the summary was not
        summary = None
        if not files:
            summary = f"[SKIP] {name} (no input files)"
        elif not ran_any:
            summary = f"[SKIP] {name} ({len(files)} file(s) up to date)"
        if summary:
            lines.append(summary)
            self._emit([summary])
        return lines

    @staticmethod
    def _ensure_output_dirs(outputs, depfile, project_dir):
        """mkdir -p for per-file outputs (obj/ etc)"""
        for path in list(outputs) + ([depfile] if depfile else []):
            parent = os.path.dirname(os.path.join(project_dir, path))
            if parent:
                os.makedirs(parent, exist_ok=True)

    def _run_one(self, key, label, command_line, sources, outputs,
                 project_dir, depfile, always_run):
        """Cache-check, run, record one command"""
        lines = []

        if always_run:
            task_hash = None
        else:
            # last build's discovered headers, so an edited #include misses
            with self._cache_lock:
                prev_deps = self.cache.get_deps(key)
            hash_inputs = self._abs_all(sources + prev_deps, project_dir)
            task_hash = self.cache.compute_task_hash(command_line, hash_inputs)
            with self._cache_lock:
                cached = self.cache.is_cached(key, task_hash, project_dir)
            if cached:
                lines.append(f"[SKIP] {label} (up to date)")
                self._emit(lines)
                return lines

        self.plugins.run_hooks(f"Pre{key.split(':')[0]}", project_dir)

        if self.dry_run:
            lines.append(f"[DRY] {label}: {command_line}")
            self._emit(lines)
            return lines

        lines.append(f"[RUN]  {label}: {command_line}")
        self._emit(lines)
        result = self._execute(command_line, project_dir)

        base_name = key.split(":")[0]
        if result.returncode != 0:
            self.plugins.run_hooks(f"OnFail{base_name}", project_dir)
            raise TaskFailedError(
                f"Task '{label}' failed (exit {result.returncode})")

        self.plugins.run_hooks(f"Post{base_name}", project_dir)

        if task_hash is not None:
            deps = []
            if depfile:
                deps = parse_depfile(os.path.join(project_dir, depfile))
                # re-hash with them so the next run's digest matches
                hash_inputs = self._abs_all(sources + deps, project_dir)
                task_hash = self.cache.compute_task_hash(command_line, hash_inputs)
            with self._cache_lock:
                self.cache.update(key, task_hash, outputs=outputs, deps=deps)

        return lines

    @staticmethod
    def _abs_all(paths, project_dir):
        """Absolute, de-duplicated hash inputs"""
        seen = []
        for p in paths:
            full = p if os.path.isabs(p) else os.path.join(project_dir, p)
            if full not in seen:
                seen.append(full)
        return seen

    def _assemble(self, cmd_str, flags_str, files, output_str):
        """Join prefix / command / flags / files / output"""
        segments = []
        prefix = self.plugins.command_prefix.strip()
        if prefix and not cmd_str.strip().startswith(prefix):
            segments.append(prefix)
        if cmd_str.strip():
            segments.append(cmd_str.strip())
        if flags_str.strip():
            segments.append(flags_str.strip())
        if files:
            segments.append(" ".join(files))
        if output_str.strip():
            segments.append(output_str.strip())
        return " ".join(segments)

    @staticmethod
    def _execute(command_line, project_dir):
        """Run a command, routing around Git Bash on Windows.

        shell=True picks up bash there, which breaks cmd.exe syntax, so
        'cmd /c' is stripped and re-run through COMSPEC instead.
        """
        abs_dir = os.path.abspath(project_dir)

        if command_line.lower().startswith("cmd /c ") or \
           command_line.lower().startswith("cmd.exe /c "):
            rest = re.sub(r'^cmd(?:\.exe)?\s+/c\s+', '', command_line,
                          flags=re.IGNORECASE)
            if os.name == "nt":
                # cmd.exe needs ".\main.exe" or a full path, not "main.exe"
                exe = rest.split()[0] if rest.split() else ""
                if exe and not os.path.isabs(exe) and \
                   not exe.startswith(".") and \
                   "\\" not in exe and "/" not in exe and \
                   exe.lower().endswith(('.exe', '.bat', '.cmd', '.com')):
                    abs_exe = os.path.join(abs_dir, exe)
                    if os.path.isfile(abs_exe):
                        rest_parts = rest.split()
                        rest_parts[0] = abs_exe
                        rest = " ".join(rest_parts)
                return subprocess.run(
                    rest, shell=True, cwd=abs_dir,
                    executable=os.environ.get("COMSPEC", "cmd.exe"),
                )
            return subprocess.run(command_line, shell=True, cwd=abs_dir)

        if os.name == "nt":
            parts = command_line.split()
            if parts:
                exe = parts[0]
                # resolve local exes against the project dir
                if not os.path.isabs(exe) and \
                   not any(c in exe for c in '/\\') and \
                   exe.lower().endswith(('.exe', '.bat', '.cmd', '.com')):
                    abs_exe = os.path.join(abs_dir, exe)
                    if os.path.isfile(abs_exe):
                        return subprocess.run(
                            [abs_exe] + parts[1:], cwd=abs_dir,
                        )
            return subprocess.run(
                command_line, shell=True, cwd=abs_dir,
                executable=os.environ.get("COMSPEC", "cmd.exe"),
            )

        return subprocess.run(command_line, shell=True, cwd=abs_dir)

    @staticmethod
    def _extract_output_paths(output_str, project_dir):
        """Output paths from the Output field: -o <path> or -o<path>"""
        paths = []
        for m in re.finditer(r'-o\s+(\S+)', output_str):
            paths.append(m.group(1))
        for m in re.finditer(r'-o(\S+)', output_str):
            paths.append(m.group(1))
        return paths


# TOML
def parse_toml(filepath):
    """Parse a project/mold file into a plain nested dict"""
    if not os.path.exists(filepath):
        raise ConfigNotFoundError(f"Missing file: {filepath}")
    try:
        with open(filepath, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigParseError(f"Could not parse {filepath}:\n  {e}") from e


def load_variables(config):
    """Raw (unresolved) [variables] table"""
    return dict(config.get("variables", {}))


def load_profiles(config):
    """Raw (unresolved) [profiles.*] tables"""
    return {name: dict(vals) for name, vals in config.get("profiles", {}).items()}


def mold_search_path(project_dir, config=None):
    """Mold dirs in priority order: local wins, bundled last"""
    yield project_dir

    if config is not None:
        declared = config.get("meta", {}).get("mold_path", "")
        for part in declared.split(os.pathsep):
            if part.strip():
                yield os.path.join(project_dir, part.strip())

    for part in os.environ.get("BLOOMERY_MOLD_PATH", "").split(os.pathsep):
        if part.strip():
            yield part.strip()

    yield os.path.join(os.path.expanduser("~"), ".bloomery", "molds")
    yield os.path.join(os.path.dirname(os.path.abspath(__file__)), "molds")


def load_mold(system_name, project_dir, config=None, _seen=None):
    """Load a mold, following Extends. Missing is an error, not a warning"""
    if not system_name:
        return None

    searched = []
    for directory in mold_search_path(project_dir, config):
        candidate = os.path.join(directory, f"{system_name.lower()}.toml")
        searched.append(candidate)
        if os.path.exists(candidate):
            mold = parse_toml(candidate)
            return _apply_mold_inheritance(
                mold, system_name, project_dir, config, _seen)

    raise MoldNotFoundError(
        "Mold not found: {}\n  Searched:\n{}".format(
            system_name, "\n".join(f"    {p}" for p in searched))
    )


def _apply_mold_inheritance(mold, name, project_dir, config, seen):
    """Merge parent [definitions] under the child's"""
    parent_name = mold.get("bloomery", {}).get("extends", "").strip()
    if not parent_name:
        return mold

    seen = seen or []
    if name.lower() in [s.lower() for s in seen]:
        raise MoldNotFoundError(
            f"Cyclic mold inheritance: {' -> '.join(seen + [name])}")

    parent = load_mold(parent_name, project_dir, config, _seen=seen + [name])
    if parent is None or "definitions" not in parent:
        return mold

    defs = mold.setdefault("definitions", {})
    for key, value in parent["definitions"].items():
        defs.setdefault(key, value)
    return mold


def list_targets(config):
    """Print available targets from [tasks]."""
    tasks = config.get("tasks", {})
    if not tasks:
        print("No tasks defined.")
        return

    print("Available targets:")
    for name, task in tasks.items():
        deps = task.get("depends", [])
        dep_str = f"  (depends: {', '.join(deps)})" if deps else ""
        print(f"  {name}{dep_str}")


# CLI
def main():
    parser = argparse.ArgumentParser(
        prog="bloomery",
        description="Bloomery — A TOML-native Build System",
        epilog="Self-management: bloomery install | update | uninstall",
    )
    parser.add_argument("project", help="Path to project .toml file")
    parser.add_argument("targets", nargs="*", help="Specific targets to run")
    parser.add_argument("--clean", action="store_true",
                        help="Force full rebuild (ignore cache)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show commands without executing")
    parser.add_argument("--list", action="store_true",
                        help="List available targets and exit")
    parser.add_argument("--verbose", action="store_true",
                        help="Show resolution details")
    parser.add_argument("-D", action="append", default=[], metavar="VAR=VALUE",
                        help="Define/override a variable (e.g. -D debug=true)")
    parser.add_argument("--profile", default=None,
                        help="Activate a named profile")
    parser.add_argument("-j", "--jobs", type=int, default=1, metavar="N",
                        help="Run up to N independent tasks in parallel "
                             "(default 1; 0 = one per CPU)")
    parser.add_argument("--keep-going", action="store_true",
                        help="With -j, let running tasks finish after a failure")
    parser.add_argument("--version", action="version",
                        version=f"bloomery {__version__}")
    args = parser.parse_args()

    project_path = os.path.abspath(args.project)
    project_dir = os.path.dirname(project_path) or "."
    config = parse_toml(project_path)

    if args.list:
        list_targets(config)
        return

    # [variables] < profile < CLI
    variables = load_variables(config)

    if args.profile:
        profiles = load_profiles(config)
        if args.profile in profiles:
            variables.update(profiles[args.profile])
        else:
            print(f"[WARN] Unknown profile: {args.profile}")
            print(f"       Available: {', '.join(profiles.keys()) or '(none)'}")

    cli_vars = {}
    for d in args.D:
        if "=" in d:
            k, v = d.split("=", 1)
            cli_vars[k.strip()] = v.strip()
        else:
            cli_vars[d.strip()] = "true"

    system_name = config.get("meta", {}).get("system", "")
    mold_config = load_mold(system_name, project_dir, config)

    ctx = Context(
        project_dir=project_dir,
        variables=variables,
        mold_config=mold_config,
        project_config=config,
        verbose=args.verbose,
        cli_vars=cli_vars,
    )
    evaluator = Evaluator(ctx)

    # infer a default 'output' var from the first literal "-o <path>" found
    if "output" not in variables and "output" not in cli_vars:
        for task in config.get("tasks", {}).values():
            out = task.get("output")
            if isinstance(out, str):
                m = re.search(r'-o\s+(\S+)', out)
                if m:
                    ctx.variables.setdefault("output", m.group(1))

    plugins = PluginManager(config, evaluator)
    plugins.load()

    dag = TaskDAG()
    dag.build(config)

    if args.targets:
        known_tasks = config.get("tasks", {})
        for t in args.targets:
            if t not in known_tasks:
                raise UnknownTargetError(
                    f"Unknown target: {t!r}\n"
                    f"  Available: {', '.join(sorted(known_tasks)) or '(none)'}"
                )
        order = dag.topo_sort_with_config(config, targets=args.targets)
    else:
        order = dag.topo_sort_with_config(config)

    if not order:
        print("Nothing to build.")
        return

    cache = BuildCache(project_dir)
    if args.clean:
        cache.invalidate()

    runner = TaskRunner(evaluator, cache, plugins, dry_run=args.dry_run)
    jobs = args.jobs if args.jobs > 0 else (os.cpu_count() or 1)

    print(f"{'=' * 50}")
    print(f"  Bloomery  |  {config.get('meta', {}).get('name', '?')}")
    print(f"  Platform  |  {ctx.platform}")
    print(f"  Targets   |  {' -> '.join(order)}")
    if jobs > 1:
        print(f"  Jobs      |  {jobs}")
    print(f"{'=' * 50}\n")

    # save progress even on failure, so completed tasks don't rebuild
    try:
        run_tasks(runner, evaluator, dag, config, order, project_dir,
                  jobs=jobs, keep_going=args.keep_going)
    finally:
        if not args.dry_run:
            cache.save()

    print("OK - All tasks completed.")


def run_tasks(runner, evaluator, dag, config, order, project_dir,
              jobs=1, keep_going=False):
    """Execute order, serially or in parallel waves"""
    tasks = config.get("tasks", {})
    runnable = [t for t in order if t in tasks]

    if jobs <= 1:
        for name in runnable:
            print(f"-- {name} --")
            runner.run_task(name, tasks[name], project_dir)
            print()
        return

    # each concurrent task gets its own Context/Evaluator (input/stem)
    for wave in dag.ready_waves(config, runnable):
        if len(wave) == 1:
            name = wave[0]
            print(f"-- {name} --")
            runner.run_task(name, tasks[name], project_dir)
            print()
            continue

        print(f"-- {' | '.join(wave)} --")
        failures = []
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = {
                pool.submit(
                    runner.run_task, name, tasks[name], project_dir,
                    Evaluator(evaluator.ctx.fork()),
                ): name
                for name in wave
            }
            for future in futures:
                try:
                    future.result()
                except TaskFailedError as e:
                    failures.append(e)
                    if not keep_going:
                        for pending in futures:
                            pending.cancel()
        print()
        if failures:
            raise failures[0]


# SELF
REPO_URL = "https://github.com/hydrophobis/Bloomery"
DEV_DIR = os.path.join(os.path.expanduser("~"), ".bloomery", "src")


def repo_root():
    """The git checkout backing this install, or None"""
    for candidate in (os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      DEV_DIR):
        if os.path.isdir(os.path.join(candidate, ".git")):
            return candidate
    return None


def _run(*cmd):
    print("+", " ".join(cmd))
    if subprocess.run(list(cmd)).returncode != 0:
        raise BloomeryError(f"command failed: {' '.join(cmd)}")


def install_command():
    """pip install -e the checkout, cloning one if needed"""
    root = repo_root()
    if root is None:
        os.makedirs(os.path.dirname(DEV_DIR), exist_ok=True)
        _run("git", "clone", REPO_URL, DEV_DIR)
        root = DEV_DIR
    _run(sys.executable, "-m", "pip", "install", "-e", root)
    print(f"OK - 'bloomery' installed in dev mode from {root}")


def update_command():
    """Git pull for a dev checkout, else upgrade the installed package"""
    root = repo_root()
    if root is not None:
        _run("git", "-C", root, "pull", "--ff-only")
        print(f"OK - updated {root}")
        return
    _run(sys.executable, "-m", "pip", "install", "--upgrade", "bloomery-build")
    print("OK - upgraded bloomery-build from PyPI")


def uninstall_command():
    _run(sys.executable, "-m", "pip", "uninstall", "-y", "bloomery-build")


SELF_COMMANDS = {
    "install": install_command,
    "update": update_command,
    "uninstall": uninstall_command,
}


def cli():
    """Entry point: run main(), report errors without a traceback"""
    try:
        # before argparse, which would read these as a project path
        if len(sys.argv) == 2 and sys.argv[1] in SELF_COMMANDS:
            SELF_COMMANDS[sys.argv[1]]()
            return 0
        main()
    except BloomeryError as e:
        print(f"\nX {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nX Interrupted.", file=sys.stderr)
        sys.exit(130)
    return 0


if __name__ == "__main__":
    cli()
