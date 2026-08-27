"""
DSL:
    <var(name)>                              Variable reference
    <env(name)>                              Environment variable
    <shell(cmd)>                             Shell command substitution
    <mold()>                                 Insert mold's value for current field
    <mold(none)>                             Explicit no-mold (empty)
    <mold(FieldName)>                        Specific mold field
    <files ending (.ext1|.ext2)>             Match files by extension
    <files matching (glob)>                  Match files by glob pattern
    <files in (dir; .ext1|.ext2)>            Directory-scoped extension match
    <platform>                               Current platform string
    <exists(path)>                           File existence check to "true"/"false"
    <input>                                  Primary input file stem
    <stem>                                   Current file's stem (per-file mode)
    <if(cond)>...<elif(cond)>...<else>...<end>   Conditional
    <for(var in list)>...<end>                    Loop expansion

Interpolation:
    {varname}          Variable interpolation
    {env.NAME}         Environment variable interpolation
    {mold.Field}       Mold definition lookup
    {task.T.Field}     Another task's field
    {task.T.outputs}   Files another task produced

Task fields:
    Mode = per-file    One command per input file, cached individually
    Headers = …        Extra files hashed but kept off the command line
    DepFile = …        Make-format depfile to read #include deps from

Condition syntax (for <if>):
    platform=value           Platform equality
    platform!=value           Platform inequality
    var(name)                 Variable truthiness
    var(name)=value           Variable equality
    var(name)!=value          Variable inequality
    env(name)                 Env var truthiness
    env(name)=value           Env var equality
    !condition                Negation

Loop syntax (for <for>):
    <for(x in a|b|c)>...<end>           Pipe-separated list
    <for(f in files: .cpp|.cc)>...<end>  Iterate over matching files
    <for(i in range(1,10))>...<end>      Numeric range

Usage:
    bloomery <project.ini> [targets...] [options]
    python -m bloomery <project.ini> [targets...] [options]

Options:
    --clean          Force full rebuild (ignore cache)
    --dry-run        Show commands without executing
    --list           List available targets and exit
    --verbose        Show resolution details
    -D VAR=VALUE     Define/override a variable
    --profile NAME   Activate a profile (sets variables from [Profiles.X])
    -j, --jobs N     Run independent tasks in parallel (0 = one per CPU)
    --keep-going     Don't cancel sibling tasks after a failure
    --version        Print version and exit

Self-management:
    bloomery install     pip install -e the git checkout (dev mode)
    bloomery update      git pull a dev checkout, else pip upgrade
    bloomery uninstall   pip uninstall bloomery-build
"""

__version__ = "0.1.1"

import configparser
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

# const
CACHE_FILE = ".bloomery_cache.json"
MAX_RESOLUTION_DEPTH = 12

# except
class BloomeryError(Exception):
    """Base exception for Bloomery build errors."""


class CyclicDependencyError(BloomeryError):
    """Raised when a cycle is detected in the task dependency graph."""


class TaskFailedError(BloomeryError):
    """Raised when a task command exits with a non-zero code."""


class ConfigNotFoundError(BloomeryError):
    """Raised when a project or mold INI file cannot be found."""


class ConfigParseError(BloomeryError):
    """Raised when an INI file is malformed."""


class UnknownTargetError(BloomeryError):
    """Raised when a requested target matches no task."""


class MoldNotFoundError(ConfigNotFoundError):
    """Raised when a mold cannot be located on the search path."""

# ctx class
class Context:
    """Holds all state needed during directive resolution."""

    def __init__(self, project_dir=".", variables=None, mold_config=None,
                 project_config=None, verbose=False, cli_vars=None):
        self.project_dir = project_dir
        self.variables = dict(variables or {})
        self.mold_config = mold_config
        self.project_config = project_config
        self.platform = self._detect_platform()
        self.verbose = verbose
        self.resolved_files = {}     # task_name to [file_paths]
        self.resolved_outputs = {}   # task_name to [output_paths]
        self.current_task = None
        self.current_field = None

        # CLI overrides take highest precedence
        if cli_vars:
            self.variables.update(cli_vars)

    def fork(self):
        """Return a copy for one task to mutate in isolation."""
        clone = Context.__new__(Context)
        clone.__dict__.update(self.__dict__)
        clone.variables = dict(self.variables)
        clone.current_task = None
        clone.current_field = None
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

# DSL resolution/iteration
class DirectiveEngine:
    # Regex for boundary tokens
    _CONTROL_OPEN_RE = re.compile(r'<(if|elif|for)\(')
    _CONTROL_SINGLE_RE = re.compile(r'<(else|end)>')

    def __init__(self, ctx: Context):
        self.ctx = ctx
        self._resolving_mold = False   # recursion guard

        # Handler registry: name to callable(args_str | None) to str
        self._handlers = {
            "var":            self._handle_var,
            "env":            self._handle_env,
            "shell":          self._handle_shell,
            "mold":           self._handle_mold,
            "files ending":   self._handle_files_ending,
            "files matching": self._handle_files_matching,
            "files in":       self._handle_files_in,
            "platform":       self._handle_platform,
            "exists":         self._handle_exists,
            "input":          self._handle_input,
            "output":         self._handle_output,
            "stem":           self._handle_stem,
        }

        self._directive_re = self._build_directive_regex()

    # public API

    def register_handler(self, name, handler):
        """Register a custom directive handler (extensible via plugins)."""
        self._handlers[name] = handler
        self._directive_re = self._build_directive_regex()

    def resolve(self, text, field_name=None):
        """Main entry: resolve all metaprogramming in a string value."""
        old_field = self.ctx.current_field
        self.ctx.current_field = field_name

        try:
            result = text
            for _ in range(MAX_RESOLUTION_DEPTH):
                prev = result
                result = self._strip_comments(result)
                result = self._resolve_control_flow(result)
                result = self._resolve_simple_directives(result)
                result = self._resolve_interpolation(result)
                if result == prev:
                    break
            return result
        finally:
            self.ctx.current_field = old_field

    @staticmethod
    def _strip_comments(text):
        return re.sub(r'\s+(?:#|;)\s+.*$', '', text, count=1, flags=re.DOTALL)

    def _resolve_control_flow(self, text):
        """Iteratively resolve <if> and <for> blocks until stable."""
        prev = None
        while text != prev:
            prev = text
            text = self._resolve_one_if(text)
            text = self._resolve_one_for(text)
        return text

    def _resolve_one_if(self, text):
        """Find and resolve the first (leftmost) <if>...<end> block."""
        # balanced matching
        tok = self._next_control_token(text, 0)
        if tok is None or tok[0] != "if":
            return text

        if_start = tok[2]
        if_open_end = tok[3]
        if_condition = tok[1]

        branches = [(if_condition, "")]
        depth = 1
        pos = if_open_end
        branch_start = if_open_end

        while pos < len(text):
            tok = self._next_control_token(text, pos)
            if tok is None:
                break                                   # no <end>

            ttype, targ, tstart, tend = tok

            if ttype in ("if", "for"):
                depth += 1
                pos = tend
            elif ttype == "end":
                depth -= 1
                if depth == 0:
                    branches[-1] = (branches[-1][0], text[branch_start:tstart])
                    chosen = self._evaluate_if_branches(branches)
                    return text[:if_start] + chosen + text[tend:]
                pos = tend
            elif depth == 1 and ttype == "elif":
                branches[-1] = (branches[-1][0], text[branch_start:tstart])
                branch_start = tend
                branches.append((targ, ""))
                pos = tend
            elif depth == 1 and ttype == "else":
                branches[-1] = (branches[-1][0], text[branch_start:tstart])
                branch_start = tend
                branches.append((None, ""))            # None = else (always true)
                pos = tend
            else:
                pos = tend

        return text                                      # no matching <end>

    def _evaluate_if_branches(self, branches):
        """Pick the first truthy branch."""
        for condition, content in branches:
            if condition is None:                         # <else>
                return content
            if self._eval_condition(condition):
                return content
        return ""

    def _resolve_one_for(self, text):
        """Find and resolve the first <for(var in list)>...<end> block."""
        tok = self._next_control_token(text, 0)
        if tok is None or tok[0] != "for":
            return text

        for_start = tok[2]
        for_open_end = tok[3]
        for_spec = tok[1]

        var_name, iterable = self._parse_for_spec(for_spec)

        # Find matching <end>
        depth = 1
        pos = for_open_end

        while pos < len(text):
            tok = self._next_control_token(text, pos)
            if tok is None:
                break

            ttype, _targ, tstart, tend = tok
            if ttype in ("if", "for"):
                depth += 1
                pos = tend
            elif ttype == "end":
                depth -= 1
                if depth == 0:
                    body = text[for_open_end:tstart]
                    expanded = self._expand_for_loop(var_name, iterable, body)
                    return text[:for_start] + expanded + text[tend:]
                pos = tend
            else:
                pos = tend

        return text

    def _parse_for_spec(self, spec):
        """Parse 'var in list' to (var_name, iterable_spec)."""
        m = re.match(r'(\w+)\s+in\s+(.+)', spec.strip())
        if not m:
            return ("item", spec)
        return m.group(1), m.group(2).strip()

    def _expand_for_loop(self, var_name, iterable_spec, body):
        """Expand a <for> loop by iterating and resolving the body."""
        items = self._resolve_iterable(iterable_spec)

        parts = []
        saved = self.ctx.variables.get(var_name)
        for item in items:
            self.ctx.set_var(var_name, item)
            expanded = self.resolve(body)
            parts.append(expanded)

        # Restore var value
        if saved is not None:
            self.ctx.set_var(var_name, saved)
        elif var_name in self.ctx.variables:
            del self.ctx.variables[var_name]

        return " ".join(parts)

    def _resolve_iterable(self, spec):
        """Turn an iterable spec into a list of strings."""
        if spec.startswith("files:"):
            exts_str = spec[6:].strip()
            exts = [e.strip() for e in exts_str.split("|")]
            files = []
            for ext in exts:
                pattern = os.path.join(self.ctx.project_dir, f"*{ext}")
                files.extend(glob.glob(pattern))
            return sorted(set(
                os.path.relpath(f, self.ctx.project_dir) for f in files
            ))

        # range(start, end[, step])
        if spec.startswith("range(") and spec.endswith(")"):
            inner = spec[6:-1]
            parts = [int(p.strip()) for p in inner.split(",")]
            if len(parts) == 1:
                nums = range(parts[0])
            elif len(parts) == 2:
                nums = range(parts[0], parts[1])
            else:
                nums = range(parts[0], parts[1], parts[2])
            return [str(i) for i in nums]

        return [item.strip() for item in spec.split("|") if item.strip()]

    # helpers
    @classmethod
    def _next_control_token(cls, text, start):
        """Find the next control-flow token from *start*.

        Handles nested parentheses in conditions like
        ``<if(var(debug))>`` by counting balanced parens.
        """
        m = cls._CONTROL_SINGLE_RE.search(text, start)
        single_start = m.start() if m else len(text)

        m2 = cls._CONTROL_OPEN_RE.search(text, start)
        open_start = m2.start() if m2 else len(text)

        if m2 and m2.start() <= single_start:
            ttype = m2.group(1)
            abs_start = m2.start()
            paren_start = m2.end() - 1

            depth = 1
            pos = paren_start + 1
            while pos < len(text) and depth > 0:
                if text[pos] == '(':
                    depth += 1
                elif text[pos] == ')':
                    depth -= 1
                pos += 1

            if pos < len(text) and text[pos] == '>':
                abs_end = pos + 1
                arg = text[paren_start + 1 : pos - 1]
                return (ttype, arg, abs_start, abs_end)

            arg = text[paren_start + 1 : pos - 1] if depth == 0 else ""
            return (ttype, arg, abs_start, pos)

        if m:
            return (m.group(1), None, m.start(), m.end())

        return None

    def _build_directive_regex(self):
        """Dynamically build a regex from the handler registry.

        Allows optional whitespace between the directive name and
        the opening paren of its argument list.
        """
        parts = []
        for name in self._handlers:
            words = name.split()
            escaped = [re.escape(w) for w in words]
            parts.append(r"\s+".join(escaped))
        name_pat = "|".join(parts)

        return re.compile(
            r"<(" + name_pat + r")(?:\s*\(([^)]*)\))?>"
        )

    def _resolve_simple_directives(self, text):
        """Resolve every self-closing directive token in *text*."""
        def _replacer(m):
            name = re.sub(r"\s+", " ", m.group(1)).strip()
            args = m.group(2)

            handler = self._handlers.get(name)
            if handler is None:
                if self.ctx.verbose:
                    print(f"  [WARN] Unknown directive: <{name}>")
                return m.group(0)

            result = handler(args)
            if self.ctx.verbose and name != "platform":
                arg_display = f"({args})" if args is not None else ""
                print(f"  [RESOLVE] <{name}{arg_display}> -> {result!r}")
            return result

        return self._directive_re.sub(_replacer, text)

    _INTERP_RE = re.compile(r'\{([^}]+)\}')

    def _resolve_interpolation(self, text):
        """Resolve {expr} interpolation tokens."""
        def _replacer(m):
            expr = m.group(1).strip()

            # {env.NAME}
            if expr.startswith("env."):
                env_name = expr[4:]
                return os.environ.get(env_name, "")

            # {mold.Field}
            if expr.startswith("mold."):
                return self._handle_mold(expr[5:])

            # {task.TaskName.Field}
            if expr.startswith("task."):
                parts = expr[5:].split(".", 1)
                if len(parts) == 2:
                    task, field = parts
                    return self._get_task_field(task, field)
                return ""

            # {varname}
            return self.ctx.get_var(expr)

        return self._INTERP_RE.sub(_replacer, text)

    def _get_task_field(self, task_name, field_name):
        if field_name == "outputs":
            return " ".join(self.ctx.resolved_outputs.get(task_name, []))

        if self.ctx.project_config is None:
            return ""
        section = f"Tasks.{task_name}"
        if self.ctx.project_config.has_section(section):
            raw = self.ctx.project_config.get(section, field_name, fallback="")
            return self.resolve(raw, field_name=field_name)
        return ""

    def _eval_condition(self, cond_str):
        """Evaluate a condition from <if(cond)>."""
        cond = cond_str.strip()

        negate = False
        if cond.startswith("!"):
            negate = True
            cond = cond[1:].strip()

        result = self._eval_positive_condition(cond)
        return not result if negate else result

    def _eval_positive_condition(self, cond):
        """Evaluate a positive (non-negated) condition."""
        # platform = value  |  platform != value
        m = re.match(r'platform\s*(!=|=|==)\s*(\w+)$', cond)
        if m:
            op, val = m.group(1), m.group(2)
            if op == "!=":
                return self.ctx.platform != val
            return self.ctx.platform == val

        # var(name) [= value | != value]
        m = re.match(r'var\((\w+)\)\s*(?:(!=|=|==)\s*(.+))?$', cond)
        if m:
            name, op, expected = m.group(1), m.group(2), m.group(3)
            actual = self.ctx.get_var(name)
            if op is None:
                return bool(actual) and actual.lower() not in ("false", "0", "no")
            if op == "!=":
                return actual != expected
            return actual == expected

        # env(name) [= value | != value]
        m = re.match(r'env\((\w+)\)\s*(?:(!=|=|==)\s*(.+))?$', cond)
        if m:
            name, op, expected = m.group(1), m.group(2), m.group(3)
            actual = os.environ.get(name, "")
            if op is None:
                return bool(actual)
            if op == "!=":
                return actual != expected
            return actual == expected

        # exists(path)
        m = re.match(r'exists\((.+)\)$', cond)
        if m:
            path = m.group(1).strip()
            return os.path.exists(os.path.join(self.ctx.project_dir, path))

        # Bare truthiness
        return bool(cond) and cond.lower() not in ("false", "0", "no")

    def _handle_var(self, args):
        """<var(name)> to variable value."""
        if args is None:
            return ""
        return self.ctx.get_var(args.strip())

    def _handle_env(self, args):
        """<env(name)> to environment variable value."""
        if args is None:
            return ""
        return os.environ.get(args.strip(), "")

    def _handle_shell(self, args):
        """<shell(cmd)> to stdout of *cmd*, trimmed."""
        if args is None:
            return ""
        try:
            result = subprocess.run(
                args, shell=True, capture_output=True, text=True,
                timeout=30, cwd=self.ctx.project_dir
            )
            return result.stdout.strip()
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            if self.ctx.verbose:
                print(f"  [WARN] <shell> failed: {args!r}: {e}")
            return ""

    def _handle_mold(self, args):
        """<mold()> / <mold(none)> / <mold(FieldName)> to mold value."""
        if self._resolving_mold:
            return ""                                    # recursion guard

        if args is None or args.strip() == "":
            field = self.ctx.current_field
        elif args.strip().lower() == "none":
            return ""
        else:
            field = args.strip()

        if self.ctx.mold_config is None or field is None:
            return ""

        try:
            mold_section = self.ctx.mold_config["Definitions"]
        except KeyError:
            return ""

        if field not in mold_section:
            return ""

        mold_value = mold_section[field]

        overlay = {}
        for key, value in mold_section.items():
            for alias in (key, key.lower()):
                if alias not in self.ctx.variables:
                    overlay[alias] = value

        self.ctx.variables.update(overlay)
        self._resolving_mold = True
        try:
            resolved = self.resolve(mold_value, field_name=field)
        finally:
            self._resolving_mold = False
            for alias in overlay:
                self.ctx.variables.pop(alias, None)
        return resolved

    def _handle_files_ending(self, args):
        """<files ending (.ext1|.ext2)> to space separated file list"""
        if args is None:
            return ""
        exts = [e.strip() for e in args.split("|")]
        files = []
        for ext in exts:
            pattern = os.path.join(self.ctx.project_dir, f"*{ext}")
            files.extend(glob.glob(pattern))
        rel = sorted(set(
            os.path.relpath(f, self.ctx.project_dir) for f in files
        ))
        return " ".join(rel)

    def _handle_files_matching(self, args):
        """<files matching (glob)> to space separated file list"""
        if args is None:
            return ""
        pattern = os.path.join(self.ctx.project_dir, args.strip())
        files = glob.glob(pattern, recursive=True)
        rel = sorted(set(
            os.path.relpath(f, self.ctx.project_dir) for f in files
        ))
        return " ".join(rel)

    def _handle_files_in(self, args):
        """<files in (dir; .ext1|.ext2)> to directory scoped file list"""
        if args is None:
            return ""
        parts = args.split(";", 1)
        directory = parts[0].strip()
        exts_str = parts[1].strip() if len(parts) > 1 else ""
        exts = [e.strip() for e in exts_str.split("|")]

        files = []
        for ext in exts:
            pattern = os.path.join(self.ctx.project_dir, directory, f"*{ext}")
            files.extend(glob.glob(pattern))

        rel = sorted(set(
            os.path.relpath(f, self.ctx.project_dir) for f in files
        ))
        return " ".join(rel)

    def _handle_platform(self, _args):
        """<platform> to 'windows' | 'linux' | 'macos' """
        return self.ctx.platform

    def _handle_exists(self, args):
        """<exists(path)> to 'true' | 'false' """
        if args is None:
            return "false"
        path = os.path.join(self.ctx.project_dir, args.strip())
        return "true" if os.path.exists(path) else "false"

    def _handle_input(self, _args):
        """<input> to primary input stem name"""
        return self.ctx.get_var("input", "")

    def _handle_output(self, _args):
        """<output> to primary output name"""
        return self.ctx.get_var("output", "")

    def _handle_stem(self, _args):
        """<stem> to current file's stem in per-file mode."""
        return self.ctx.get_var("stem", "")

    def _fork_engine(self, ctx):
        """Clone this engine onto ctx, preserving registered handlers"""
        clone = DirectiveEngine(ctx)
        clone._handlers = dict(self._handlers)
        clone._directive_re = clone._build_directive_regex()
        return clone

# CACHE
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


class BuildCache:
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
        stored_hash = entry.get("hash", "")
        if stored_hash != task_hash:
            return False
        outputs = entry.get("outputs", [])
        for out_path in outputs:
            full = os.path.join(project_dir, out_path) if project_dir else out_path
            if not os.path.exists(full):
                return False
        return True

    def get_deps(self, task_name):
        """Return header dependencies recorded by the previous build."""
        entry = self.data.get(task_name)
        if isinstance(entry, dict):
            return list(entry.get("deps", []))
        return []

    def update(self, task_name, task_hash, outputs=None, deps=None):
        """Store task hash + output file list + discovered dependencies."""
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
class TaskDAG:
    def __init__(self):
        self.graph = defaultdict(list)     # task to [dependents]
        self.indegree = defaultdict(int)
        self.tasks = []                     # all task section names

    def build(self, config):
        if "Tasks" not in config:
            return self

        for _alias, section_name in config["Tasks"].items():
            section_name = section_name.strip()
            if section_name not in self.tasks:
                self.tasks.append(section_name)

            section_key = f"Tasks.{section_name}"
            if not config.has_section(section_key):
                continue

            depends_raw = config.get(section_key, "Depends", fallback="")
            deps = [d.strip() for d in depends_raw.split(",") if d.strip()]

            for dep in deps:
                self.graph[dep].append(section_name)
                self.indegree[section_name] = self.indegree.get(section_name, 0) + 1

            if section_name not in self.indegree:
                self.indegree[section_name] = 0

        return self

    def get_dependencies(self, task_name, config):
        """Direct dependencies of task_name"""
        section_key = f"Tasks.{task_name}"
        if not config.has_section(section_key):
            return []
        raw = config.get(section_key, "Depends", fallback="")
        return [d.strip() for d in raw.split(",") if d.strip()]

    def topo_sort_with_config(self, config, targets=None):
        """Topo sort. targets pulls in transitive deps, else the default set"""
        if targets:
            needed = self._collect_deps(targets, config)
        else:
            needed = self._default_tasks(config)

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
        depended_on = set()
        for t in self.tasks:
            for dep in self.get_dependencies(t, config):
                depended_on.add(dep)

        needed = set()
        needed.update(depended_on)

        for t in self.tasks:
            deps = self.get_dependencies(t, config)
            if not deps:
                section_key = f"Tasks.{t}"
                is_default = True
                always_run = False
                if config.has_section(section_key):
                    is_default = config.get(
                        section_key, "Default", fallback="yes"
                    ).lower() not in ("no", "false", "0")
                    always_run = config.get(
                        section_key, "AlwaysRun", fallback="no"
                    ).lower() in ("yes", "true", "1")

                # AlwaysRun (e.g. Clean) only runs when asked for
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
        """All tasks roots transitively depend on"""
        needed = set()
        stack = list(roots)
        while stack:
            t = stack.pop()
            if t in needed:
                continue
            needed.add(t)
            for dep in self.get_dependencies(t, config):
                stack.append(dep)
        return needed


# PLUGINS
class PluginManager:
    def __init__(self, config, engine):
        self.config = config
        self.engine = engine
        self._prefix = None
        self._hooks = defaultdict(list)

    def load(self):
        if self.config.has_section("Plugins.Command"):
            raw = self.config.get("Plugins.Command", "Prefix", fallback="")
            self._prefix = self.engine.resolve(raw, field_name="Plugins.Command.Prefix")

        # Pre<Task> / Post<Task> / OnFail<Task>, incl. [Plugins.Hooks.X]
        loaded_sections = set()
        for section_name in self.config.sections():
            if section_name.startswith("Plugins.Hooks"):
                if section_name in loaded_sections:
                    continue
                loaded_sections.add(section_name)
                for key, value in self.config.items(section_name):
                    hook_name = key.strip()
                    hook_cmd = self.engine.resolve(value.strip())
                    self._hooks[hook_name].append(hook_cmd)

    @property
    def command_prefix(self):
        return self._prefix or ""

    def get_hooks(self, event):
        return self._hooks.get(event, [])

    def run_hooks(self, event, project_dir):
        for cmd in self.get_hooks(event):
            if self.engine.ctx.verbose:
                print(f"  [HOOK:{event}] {cmd}")
            subprocess.run(cmd, shell=True, cwd=project_dir)


# RUNNER
class TaskRunner:
    """Resolve fields, check cache, execute.

    Mode = whole (default): one command for every file at once.
    Mode = per-file: one command and one cache entry per file.
    Shared across threads, so cache/stdout are locked and every task
    runs against a forked Context.
    """

    def __init__(self, engine, cache, plugins, dry_run=False):
        self.engine = engine
        self.cache = cache
        self.plugins = plugins
        self.dry_run = dry_run
        self._cache_lock = threading.Lock()
        self._print_lock = threading.Lock()

    @staticmethod
    def _is_true(value):
        return value.strip().lower() in ("yes", "true", "1")

    def _emit(self, lines):
        """Print one task's lines atomically"""
        with self._print_lock:
            for line in lines:
                print(line)
            sys.stdout.flush()

    def _resolve_file_list(self, engine, raw, field_name):
        if not raw.strip():
            return []
        resolved = engine.resolve(raw, field_name=field_name)
        return [f for f in resolved.split() if f]

    def run_task(self, name, task_config, project_dir, engine=None):
        """Run one task, return its output lines"""
        engine = engine or self.engine
        ctx = engine.ctx
        ctx.current_task = name

        always_run = self._is_true(task_config.get("AlwaysRun", "no"))
        per_file = task_config.get("Mode", "").strip().lower() == "per-file"

        # Files first so <input> / <stem> can be set
        files = self._resolve_file_list(engine, task_config.get("Files", ""), "Files")

        # exclude before <input>, else excluded files pollute the stem
        exclude_raw = task_config.get("Exclude", "")
        if exclude_raw.strip():
            exclude_str = engine.resolve(exclude_raw, field_name="Exclude")
            patterns = [p for p in exclude_str.split() if p]
            files = [f for f in files
                     if not any(fnmatch.fnmatch(os.path.basename(f), p)
                                for p in patterns)]

        # hashed but never passed to the compiler: #include inputs
        headers = self._resolve_file_list(
            engine, task_config.get("Headers", ""), "Headers")

        if files:
            stem = os.path.splitext(os.path.basename(files[0]))[0]
            ctx.set_var("input", stem)

        ctx.resolved_files[name] = files

        if per_file:
            return self._run_per_file(
                name, task_config, project_dir, engine, files, headers, always_run)
        return self._run_whole(
            name, task_config, project_dir, engine, files, headers, always_run)

    def _run_whole(self, name, task_config, project_dir, engine,
                   files, headers, always_run):
        ctx = engine.ctx

        cmd_str = engine.resolve(task_config.get("Command", ""), field_name="Command")
        flags_str = engine.resolve(task_config.get("Flags", ""), field_name="Flags")
        output_str = engine.resolve(task_config.get("Output", ""), field_name="Output")

        output_var = task_config.get("OutputName", "")
        if output_var:
            ctx.set_var("output", engine.resolve(output_var, field_name="OutputName"))

        command_line = self._assemble(cmd_str, flags_str, files, output_str)
        output_files = self._extract_output_paths(output_str, project_dir)
        ctx.resolved_outputs[name] = output_files

        depfile_raw = task_config.get("DepFile", "")
        depfile = engine.resolve(depfile_raw, field_name="DepFile").strip() \
            if depfile_raw.strip() else ""

        lines = self._run_one(
            key=name, label=name, command_line=command_line,
            sources=files + headers, outputs=output_files,
            project_dir=project_dir, depfile=depfile, always_run=always_run,
        )
        return lines

    def _run_per_file(self, name, task_config, project_dir, engine,
                      files, headers, always_run):
        ctx = engine.ctx
        lines = []
        all_outputs = []
        ran_any = False

        for path in files:
            stem = os.path.splitext(os.path.basename(path))[0]
            ctx.set_var("stem", stem)
            ctx.set_var("input", stem)

            cmd_str = engine.resolve(task_config.get("Command", ""), field_name="Command")
            flags_str = engine.resolve(task_config.get("Flags", ""), field_name="Flags")
            output_str = engine.resolve(task_config.get("Output", ""), field_name="Output")

            command_line = self._assemble(cmd_str, flags_str, [path], output_str)
            outputs = self._extract_output_paths(output_str, project_dir)
            all_outputs.extend(outputs)

            depfile_raw = task_config.get("DepFile", "")
            depfile = engine.resolve(depfile_raw, field_name="DepFile").strip() \
                if depfile_raw.strip() else ""

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


# INI
def parse_ini(filepath):
    """Parse an INI. Duplicate sections merge, later keys win"""
    if not os.path.exists(filepath):
        raise ConfigNotFoundError(f"Missing file: {filepath}")
    # inline comments: raw fields like Meta.System never hit _strip_comments
    config = configparser.ConfigParser(
        strict=False, inline_comment_prefixes=(";", "#"))
    config.optionxform = str          # preserve case
    try:
        config.read(filepath, encoding="utf-8")
    except configparser.Error as e:
        raise ConfigParseError(f"Could not parse {filepath}:\n  {e}") from e
    return config


def task_aliases(config):
    """[Tasks] alias to section map. get() is (section, option), so use []"""
    if not config.has_section("Tasks"):
        return {}
    return dict(config["Tasks"])


def load_variables(config):
    """Extract all variables from the [Variables] section."""
    variables = {}
    if config.has_section("Variables"):
        for key, value in config.items("Variables"):
            variables[key.strip()] = value.strip()
    return variables


def load_profiles(config):
    """Load all profile section variable sets."""
    profiles = {}
    for section in config.sections():
        if section.startswith("Profiles."):
            profile_name = section[len("Profiles."):]
            profiles[profile_name] = dict(config.items(section))
    return profiles


def mold_search_path(project_dir, config=None):
    """Mold dirs in priority order: local wins, bundled last"""
    yield project_dir

    if config is not None:
        declared = config.get("Meta", "MoldPath", fallback="")
        for part in declared.split(os.pathsep):
            if part.strip():
                yield os.path.join(project_dir, part.strip())

    for part in os.environ.get("BLOOMERY_MOLD_PATH", "").split(os.pathsep):
        if part.strip():
            yield part.strip()

    yield os.path.join(os.path.expanduser("~"), ".bloomery", "molds")
    yield os.path.join(os.path.dirname(os.path.abspath(__file__)), "molds")


def load_mold(system_name, project_dir, config=None, _seen=None):
    """Load a mold, following Extends. Missing is an error, not a warning:
    every <mold()> would otherwise expand to nothing."""
    if not system_name:
        return None

    searched = []
    for directory in mold_search_path(project_dir, config):
        candidate = os.path.join(directory, f"{system_name.lower()}.ini")
        searched.append(candidate)
        if os.path.exists(candidate):
            mold = parse_ini(candidate)
            return _apply_mold_inheritance(
                mold, system_name, project_dir, config, _seen)

    raise MoldNotFoundError(
        "Mold not found: {}\n  Searched:\n{}".format(
            system_name,
            "\n".join(f"    {p}" for p in searched),
        )
    )


def _apply_mold_inheritance(mold, name, project_dir, config, seen):
    """Merge parent [Definitions] under the child's"""
    parent_name = mold.get("Bloomery", "Extends", fallback="").strip()
    if not parent_name:
        return mold

    seen = seen or []
    if name.lower() in [s.lower() for s in seen]:
        raise MoldNotFoundError(
            f"Cyclic mold inheritance: {' -> '.join(seen + [name])}")

    parent = load_mold(parent_name, project_dir, config, _seen=seen + [name])
    if parent is None or not parent.has_section("Definitions"):
        return mold

    if not mold.has_section("Definitions"):
        mold.add_section("Definitions")
    for key, value in parent["Definitions"].items():
        if key not in mold["Definitions"]:
            mold["Definitions"][key] = value
    return mold


def list_targets(config):
    """Print available targets from [Tasks]."""
    if "Tasks" not in config:
        print("No tasks defined.")
        return

    print("Available targets:")
    for alias, section_name in config["Tasks"].items():
        section_key = f"Tasks.{section_name}"
        deps = ""
        if config.has_section(section_key):
            deps_raw = config.get(section_key, "Depends", fallback="")
            if deps_raw.strip():
                deps = f"  (depends: {deps_raw.strip()})"
        print(f"  {alias:15s} -> {section_name}{deps}")


# CLI
def main():
    parser = argparse.ArgumentParser(
        prog="bloomery",
        description="Bloomery — A Metaprogrammable Build System",
        epilog="Self-management: bloomery install | update | uninstall",
    )
    parser.add_argument("project", help="Path to project .ini file")
    parser.add_argument("targets", nargs="*", help="Specific targets to run")
    parser.add_argument("--clean", action="store_true",
                        help="Force full rebuild (ignore cache)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show commands without executing")
    parser.add_argument("--list", action="store_true",
                        help="List available targets and exit")
    parser.add_argument("--verbose", action="store_true",
                        help="Show directive resolution details")
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
    config = parse_ini(project_path)

    if args.list:
        list_targets(config)
        return

    # [Variables] < profile < CLI
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

    system_name = config.get("Meta", "System", fallback="")
    mold_config = load_mold(system_name, project_dir, config)

    # Set default output variable from task config
    if "output" not in variables and not cli_vars.get("output"):
        # Try to infer from the first build task
        for _alias, section_name in task_aliases(config).items():
            section_key = f"Tasks.{section_name.strip()}"
            if config.has_section(section_key):
                out_raw = config.get(section_key, "Output", fallback="")
                m_out = re.search(r'-o\s+(\S+)', out_raw)
                if m_out:
                    variables.setdefault("output", m_out.group(1))

    # Create context & engine
    ctx = Context(
        project_dir=project_dir,
        variables=variables,
        mold_config=mold_config,
        project_config=config,
        verbose=args.verbose,
        cli_vars=cli_vars,
    )
    engine = DirectiveEngine(ctx)

    # Mold definitions are reached through <mold(Field)> and {mold.Field}
    # rather than being copied into the variable namespace, where keys
    # like Files/Output would shadow project variables of the same name.

    # Load plugins
    plugins = PluginManager(config, engine)
    plugins.load()

    # Build DAG
    dag = TaskDAG()
    dag.build(config)

    # Determine execution order
    if args.targets:
        alias_map = {k: v.strip() for k, v in task_aliases(config).items()}
        resolved_targets = []
        for t in args.targets:
            # A target may be given as an alias ("build") or as the task
            # section name itself ("Build").  Anything that resolves to
            # neither is a user error, not a silent no-op.
            if t in alias_map:
                resolved_targets.append(alias_map[t])
            elif config.has_section(f"Tasks.{t}"):
                resolved_targets.append(t)
            else:
                known = sorted(set(alias_map) | set(alias_map.values()))
                raise UnknownTargetError(
                    f"Unknown target: {t!r}\n"
                    f"  Available: {', '.join(known) or '(none)'}"
                )
        order = dag.topo_sort_with_config(config, targets=resolved_targets)
    else:
        order = dag.topo_sort_with_config(config)

    if not order:
        print("Nothing to build.")
        return

    # Init cache
    cache = BuildCache(project_dir)
    if args.clean:
        cache.invalidate()

    # Run
    runner = TaskRunner(engine, cache, plugins, dry_run=args.dry_run)
    jobs = args.jobs if args.jobs > 0 else (os.cpu_count() or 1)

    print(f"{'=' * 50}")
    print(f"  Bloomery  |  {config.get('Meta', 'Name', fallback='?')}")
    print(f"  Platform  |  {ctx.platform}")
    print(f"  Targets   |  {' -> '.join(order)}")
    if jobs > 1:
        print(f"  Jobs      |  {jobs}")
    print(f"{'=' * 50}\n")

    # Persist whatever progress was made even if a later task fails, so a
    # failed run doesn't force already-completed tasks to rebuild.
    try:
        run_tasks(runner, engine, dag, config, order, project_dir,
                  jobs=jobs, keep_going=args.keep_going)
    finally:
        if not args.dry_run:
            cache.save()

    print("OK - All tasks completed.")


def run_tasks(runner, engine, dag, config, order, project_dir,
              jobs=1, keep_going=False):
    """Execute *order*, serially or in parallel waves."""
    runnable = [t for t in order if config.has_section(f"Tasks.{t}")]

    def task_config(name):
        return dict(config.items(f"Tasks.{name}"))

    if jobs <= 1:
        for name in runnable:
            print(f"-- {name} --")
            runner.run_task(name, task_config(name), project_dir)
            print()
        return

    # Each concurrent task gets its own Context/engine so that scratch
    # variables (<input>, <stem>, loop vars) cannot collide.
    for wave in dag.ready_waves(config, runnable):
        if len(wave) == 1:
            name = wave[0]
            print(f"-- {name} --")
            runner.run_task(name, task_config(name), project_dir)
            print()
            continue

        print(f"-- {' | '.join(wave)} --")
        failures = []
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = {
                pool.submit(
                    runner.run_task, name, task_config(name), project_dir,
                    engine._fork_engine(engine.ctx.fork()),
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
