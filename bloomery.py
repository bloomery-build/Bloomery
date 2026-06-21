"""
Bloomery — A Metaprogrammable Build System
=============================================

An INI-based build system with a powerful metaprogramming DSL.

Directives (resolved at build time):
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
    <exists(path)>                           File existence check → "true"/"false"
    <input>                                  Primary input file stem
    <if(cond)>...<elif(cond)>...<else>...<end>   Conditional
    <for(var in list)>...<end>                    Loop expansion

Interpolation:
    {varname}          Variable interpolation
    {env.NAME}         Environment variable interpolation

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
    python bloomery.py <project.ini> [targets...] [options]

Options:
    --clean          Force full rebuild (ignore cache)
    --dry-run        Show commands without executing
    --list           List available targets and exit
    --verbose        Show resolution details
    -D VAR=VALUE     Define/override a variable
    --profile NAME   Activate a profile (sets variables from [Profiles.X])
"""

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
import platform as platform_module
from collections import defaultdict, deque


# ══════════════════════════════════════════════════════════════════
#  Constants
# ══════════════════════════════════════════════════════════════════

CACHE_FILE = ".bloomery_cache.json"
MAX_RESOLUTION_DEPTH = 12


# ══════════════════════════════════════════════════════════════════
#  Exceptions
# ══════════════════════════════════════════════════════════════════

class BloomeryError(Exception):
    """Base exception for Bloomery build errors."""


class CyclicDependencyError(BloomeryError):
    """Raised when a cycle is detected in the task dependency graph."""


class TaskFailedError(BloomeryError):
    """Raised when a task command exits with a non-zero code."""


# ══════════════════════════════════════════════════════════════════
#  Context — Evaluation state carried through resolution
# ══════════════════════════════════════════════════════════════════

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
        self.resolved_files = {}     # task_name → [file_paths]
        self.current_task = None
        self.current_field = None

        # CLI overrides take highest precedence
        if cli_vars:
            self.variables.update(cli_vars)

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


# ══════════════════════════════════════════════════════════════════
#  Directive Engine — Metaprogramming template resolver
# ══════════════════════════════════════════════════════════════════

class DirectiveEngine:
    """Resolves Bloomery's metaprogramming DSL in string values.

    Processing pipeline (iterates until stable):
        1. Strip inline comments
        2. Resolve control flow  (<if>…<end>, <for>…<end>)
        3. Resolve simple directives (<var>, <env>, <shell>, …)
        4. Resolve interpolation ({expr})
    """

    # Regex for control-flow boundary tokens
    # Uses a helper method for balanced-paren matching instead of naive regex
    _CONTROL_OPEN_RE = re.compile(r'<(if|elif|for)\(')
    _CONTROL_SINGLE_RE = re.compile(r'<(else|end)>')

    def __init__(self, ctx: Context):
        self.ctx = ctx
        self._resolving_mold = False   # recursion guard

        # Handler registry  –  name → callable(args_str | None) → str
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
        }

        # Build the single-pass directive-matching regex from the registry
        self._directive_re = self._build_directive_regex()

    # ── public API ────────────────────────────────────────────────

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

    # ── comment stripping ─────────────────────────────────────────

    @staticmethod
    def _strip_comments(text):
        """Remove inline comments:  # …  or  ; …"""
        # Match ' #' or ' ;' followed by content – but only outside
        # quoted strings or directives.  Simple heuristic: strip after
        # ' # ' or ' ; ' (space+marker+space) to avoid collisions with
        # shell separators ('cd dir ; make') or URLs.
        return re.sub(r'\s+(?:#|;)\s+.*$', '', text, count=1, flags=re.DOTALL)

    # ── control-flow resolution ───────────────────────────────────

    def _resolve_control_flow(self, text):
        """Iteratively resolve <if> and <for> blocks until stable."""
        prev = None
        while text != prev:
            prev = text
            # Resolve one block per iteration; resolve <if>s before <for>s
            # so that conditions inside <for> are evaluated correctly.
            text = self._resolve_one_if(text)
            text = self._resolve_one_for(text)
        return text

    # --- <if> -----------------------------------------------------------------

    def _resolve_one_if(self, text):
        """Find and resolve the first (leftmost) <if>...<end> block."""
        # Use _next_control_token for balanced-paren matching
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
                break                                   # malformed – no <end>

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

    # --- <for> ----------------------------------------------------------------

    def _resolve_one_for(self, text):
        """Find and resolve the first <for(var in list)>...<end> block."""
        # Use _next_control_token for balanced-paren matching
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
        """Parse 'var in list' → (var_name, iterable_spec)."""
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

        # Restore original variable value
        if saved is not None:
            self.ctx.set_var(var_name, saved)
        elif var_name in self.ctx.variables:
            del self.ctx.variables[var_name]

        return " ".join(parts)

    def _resolve_iterable(self, spec):
        """Turn an iterable spec into a list of strings."""
        # files: .ext1|.ext2
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

        # Pipe-separated literal list
        return [item.strip() for item in spec.split("|") if item.strip()]

    # ── token helpers ─────────────────────────────────────────────

    @classmethod
    def _next_control_token(cls, text, start):
        """Find the next control-flow token from *start*.

        Handles nested parentheses in conditions like
        ``<if(var(debug))>`` by counting balanced parens.
        """
        # Check for <else> or <end> first (simple tokens)
        m = cls._CONTROL_SINGLE_RE.search(text, start)
        single_start = m.start() if m else len(text)

        # Check for <if( / <elif( / <for(  (open tokens with balanced parens)
        m2 = cls._CONTROL_OPEN_RE.search(text, start)
        open_start = m2.start() if m2 else len(text)

        if m2 and m2.start() <= single_start:
            # Open token — find the matching closing paren and >
            ttype = m2.group(1)
            abs_start = m2.start()
            paren_start = m2.end() - 1          # position of the '('

            # Walk forward to find the balanced closing paren
            depth = 1
            pos = paren_start + 1
            while pos < len(text) and depth > 0:
                if text[pos] == '(':
                    depth += 1
                elif text[pos] == ')':
                    depth -= 1
                pos += 1

            # pos is now right after the closing ')'; expect '>'
            if pos < len(text) and text[pos] == '>':
                abs_end = pos + 1
                arg = text[paren_start + 1 : pos - 1]   # content between ( and )
                return (ttype, arg, abs_start, abs_end)

            # Malformed — return what we have
            arg = text[paren_start + 1 : pos - 1] if depth == 0 else ""
            return (ttype, arg, abs_start, pos)

        if m:
            return (m.group(1), None, m.start(), m.end())

        return None

    # ── simple-directive resolution ──────────────────────────────

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
        # Allow optional spaces before ( and support balanced parens
        # by using a non-greedy match — the _replacer will use
        # balanced-paren parsing for multi-word directives.
        # For simple directives with no nested parens, [^)]* suffices.
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
                return m.group(0)                        # leave as-is

            result = handler(args)
            if self.ctx.verbose and name != "platform":
                arg_display = f"({args})" if args is not None else ""
                print(f"  [RESOLVE] <{name}{arg_display}> -> {result!r}")
            return result

        return self._directive_re.sub(_replacer, text)

    # ── interpolation resolution ──────────────────────────────────

    _INTERP_RE = re.compile(r'\{([^}]+)\}')

    def _resolve_interpolation(self, text):
        """Resolve {expr} interpolation tokens."""
        def _replacer(m):
            expr = m.group(1).strip()

            # {env.NAME}
            if expr.startswith("env."):
                env_name = expr[4:]
                return os.environ.get(env_name, "")

            # {task.TaskName.Field}  — cross-task reference
            if expr.startswith("task."):
                parts = expr[5:].split(".", 1)
                if len(parts) == 2:
                    task, field = parts
                    return self._get_task_field(task, field)
                return ""

            # {varname} — project variable
            return self.ctx.get_var(expr)

        return self._INTERP_RE.sub(_replacer, text)

    def _get_task_field(self, task_name, field_name):
        """Look up a field from a resolved task config."""
        if self.ctx.project_config is None:
            return ""
        section = f"Tasks.{task_name}"
        if self.ctx.project_config.has_section(section):
            raw = self.ctx.project_config.get(section, field_name, fallback="")
            return self.resolve(raw, field_name=field_name)
        return ""

    # ── condition evaluation ─────────────────────────────────────

    def _eval_condition(self, cond_str):
        """Evaluate a condition from <if(cond)>."""
        cond = cond_str.strip()

        # Negation prefix
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

    # ════════════════════════════════════════════════════════════════
    #  Directive handlers
    # ════════════════════════════════════════════════════════════════

    def _handle_var(self, args):
        """<var(name)> → variable value."""
        if args is None:
            return ""
        return self.ctx.get_var(args.strip())

    def _handle_env(self, args):
        """<env(name)> → environment variable value."""
        if args is None:
            return ""
        return os.environ.get(args.strip(), "")

    def _handle_shell(self, args):
        """<shell(cmd)> → stdout of *cmd*, trimmed."""
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
                print(f"  [WARN] <shell> failed: {args!r} — {e}")
            return ""

    def _handle_mold(self, args):
        """<mold()> / <mold(none)> / <mold(FieldName)> → mold value."""
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
        self._resolving_mold = True
        resolved = self.resolve(mold_value, field_name=field)
        self._resolving_mold = False
        return resolved

    def _handle_files_ending(self, args):
        """<files ending (.ext1|.ext2)> → space-separated file list."""
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
        """<files matching (glob)> → space-separated file list."""
        if args is None:
            return ""
        pattern = os.path.join(self.ctx.project_dir, args.strip())
        files = glob.glob(pattern, recursive=True)
        rel = sorted(set(
            os.path.relpath(f, self.ctx.project_dir) for f in files
        ))
        return " ".join(rel)

    def _handle_files_in(self, args):
        """<files in (dir; .ext1|.ext2)> → directory-scoped file list."""
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
        """<platform> → 'windows' | 'linux' | 'macos'."""
        return self.ctx.platform

    def _handle_exists(self, args):
        """<exists(path)> → 'true' | 'false'."""
        if args is None:
            return "false"
        path = os.path.join(self.ctx.project_dir, args.strip())
        return "true" if os.path.exists(path) else "false"

    def _handle_input(self, _args):
        """<input> → primary input stem name."""
        return self.ctx.get_var("input", "")

    def _handle_output(self, _args):
        """<output> → primary output name."""
        return self.ctx.get_var("output", "")


# ══════════════════════════════════════════════════════════════════
#  Cache — Content-based incremental build tracking
# ══════════════════════════════════════════════════════════════════

class BuildCache:
    """Tracks file-content hashes to skip unchanged work.

    Stores both the input hash and the expected output file list per task.
    A task is considered "cached" only when:
      - The stored input hash matches the current input hash, AND
      - All previously recorded output files still exist on disk.
    If any output is missing (e.g. manually deleted), the task re-runs.
    """

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
        """SHA-256 of file contents."""
        h = hashlib.sha256()
        try:
            with open(filepath, "rb") as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    h.update(chunk)
            return h.hexdigest()
        except (FileNotFoundError, OSError):
            return ""

    def compute_task_hash(self, command_str, files):
        """Combine command + per-file content hashes into one digest."""
        h = hashlib.sha256()
        h.update(command_str.encode())
        for filepath in sorted(files):
            h.update(filepath.encode())
            h.update(self._file_hash(filepath).encode())
        return h.hexdigest()

    def is_cached(self, task_name, task_hash, project_dir=""):
        """Check if a task is up to date.

        A task is cached only when the input hash matches AND all
        previously recorded output files still exist on disk.
        """
        entry = self.data.get(task_name)
        if entry is None:
            return False
        if isinstance(entry, str):
            # Legacy format: just a hash string, no output tracking.
            # Migrate on next update.
            return entry == task_hash
        stored_hash = entry.get("hash", "")
        if stored_hash != task_hash:
            return False
        # Verify all output files still exist
        outputs = entry.get("outputs", [])
        for out_path in outputs:
            full = os.path.join(project_dir, out_path) if project_dir else out_path
            if not os.path.exists(full):
                return False
        return True

    def update(self, task_name, task_hash, outputs=None):
        """Store task hash + output file list."""
        self.data[task_name] = {
            "hash": task_hash,
            "outputs": outputs or [],
        }

    def invalidate(self, task_name=None):
        if task_name:
            self.data.pop(task_name, None)
        else:
            self.data.clear()


# ══════════════════════════════════════════════════════════════════
#  DAG — Task dependency graph with topological sort
# ══════════════════════════════════════════════════════════════════

class TaskDAG:
    """Builds and traverses the task dependency graph."""

    def __init__(self):
        self.graph = defaultdict(list)     # task → [dependents]
        self.indegree = defaultdict(int)
        self.tasks = []                     # all task section names

    def build(self, config):
        """Construct the DAG from the [Tasks] section of *config*."""
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
        """Return the direct dependencies of *task_name*."""
        section_key = f"Tasks.{task_name}"
        if not config.has_section(section_key):
            return []
        raw = config.get(section_key, "Depends", fallback="")
        return [d.strip() for d in raw.split(",") if d.strip()]

    def topo_sort_with_config(self, config, targets=None):
        """Topo sort using *config* for dependency lookups.

        If *targets* is given, only include those tasks (and their transitive
        dependencies).  Otherwise include "default" tasks: those that are
        depended upon by other tasks, plus any task with no Depends that
        isn't explicitly a utility (e.g. Clean).
        """
        if targets:
            needed = self._collect_deps(targets, config)
        else:
            # Default build: run tasks that other tasks depend on,
            # plus any "root" task that has no Depends and is not
            # explicitly excluded via AlwaysRun or a Default=no flag.
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
        """Determine which tasks to run when no explicit targets given.

        Strategy:
          - Include tasks that are depended on by other tasks.
          - Include "leaf" tasks with no Depends whose Default field
            is not "no" / "false" and that don't have AlwaysRun=yes
            (AlwaysRun tasks only run when explicitly requested).
        """
        depended_on = set()
        for t in self.tasks:
            for dep in self.get_dependencies(t, config):
                depended_on.add(dep)

        needed = set()
        # Always include tasks that others depend on
        needed.update(depended_on)

        # Include root tasks (no Depends) that opt in to default runs
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

                # AlwaysRun tasks (like Clean) are not part of default build
                # unless explicitly requested. Normal root tasks are included.
                if is_default and not always_run:
                    needed.add(t)
            else:
                # Non-root tasks: include if they have dependencies
                # that are already in the needed set
                needed.add(t)

        return needed

    def _collect_deps(self, roots, config):
        """Recursively collect all tasks that *roots* depend on."""
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


# ══════════════════════════════════════════════════════════════════
#  Plugin Manager
# ══════════════════════════════════════════════════════════════════

class PluginManager:
    """Loads and applies build plugins from the INI config."""

    def __init__(self, config, engine):
        self.config = config
        self.engine = engine
        self._prefix = None
        self._hooks = defaultdict(list)

    def load(self):
        """Read plugin definitions from the project config."""
        # Command prefix
        if self.config.has_section("Plugins.Command"):
            raw = self.config.get("Plugins.Command", "Prefix", fallback="")
            self._prefix = self.engine.resolve(raw, field_name="Plugins.Command.Prefix")

        # Hooks:  Pre<Task> / Post<Task> / OnFail<Task>
        # Load from [Plugins.Hooks] and [Plugins.Hooks.X] subsections.
        # Use a set to track which sections we've already loaded to avoid
        # processing [Plugins.Hooks] twice (once via iteration, once via
        # the explicit check).
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
        """Return list of hook commands for *event* (e.g. 'PreBuild')."""
        return self._hooks.get(event, [])

    def run_hooks(self, event, project_dir):
        """Execute all hooks registered for *event*."""
        for cmd in self.get_hooks(event):
            if self.engine.ctx.verbose:
                print(f"  [HOOK:{event}] {cmd}")
            subprocess.run(cmd, shell=True, cwd=project_dir)


# ══════════════════════════════════════════════════════════════════
#  Task Runner — Build execution engine
# ══════════════════════════════════════════════════════════════════

class TaskRunner:
    """Resolves task fields, checks cache, and executes commands."""

    def __init__(self, engine, cache, plugins, dry_run=False):
        self.engine = engine
        self.cache = cache
        self.plugins = plugins
        self.dry_run = dry_run

    def run_task(self, name, task_config, project_dir):
        """Execute a single task.  Returns True on success."""
        ctx = self.engine.ctx
        ctx.current_task = name
        always_run = task_config.get("AlwaysRun", "no").lower() in ("yes", "true", "1")

        # 1. Resolve Files first so <input> / <output> vars can be set
        files_raw = task_config.get("Files", "")
        files_str = self.engine.resolve(files_raw, field_name="Files")
        files = [f for f in files_str.split() if f] if files_str.strip() else []

        # Apply exclude patterns BEFORE setting <input>,
        # so excluded files don't pollute the input stem.
        exclude_raw = task_config.get("Exclude", "")
        if exclude_raw.strip():
            exclude_str = self.engine.resolve(exclude_raw, field_name="Exclude")
            patterns = [p for p in exclude_str.split() if p]
            files = [f for f in files
                     if not any(fnmatch.fnmatch(os.path.basename(f), p)
                                for p in patterns)]

        # Set <input> from first non-excluded file
        if files:
            stem = os.path.splitext(os.path.basename(files[0]))[0]
            ctx.set_var("input", stem)

        ctx.resolved_files[name] = files

        # 2. Resolve remaining fields
        cmd_str = self.engine.resolve(task_config.get("Command", ""), field_name="Command")
        flags_str = self.engine.resolve(task_config.get("Flags", ""), field_name="Flags")
        output_str = self.engine.resolve(task_config.get("Output", ""), field_name="Output")

        # Set <output> variable if specified
        output_var = task_config.get("OutputName", "")
        if output_var:
            ctx.set_var("output", self.engine.resolve(output_var, field_name="OutputName"))

        # 3. Assemble full command string
        segments = []
        prefix = self.plugins.command_prefix.strip()
        # De-duplicate: if the task command already starts with the prefix, don't add it again
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

        command_line = " ".join(segments)

        # Extract output file paths for cache tracking
        output_files = self._extract_output_paths(output_str, project_dir)

        # 4. Cache check (AlwaysRun tasks always execute)
        if not always_run:
            task_hash = self.cache.compute_task_hash(command_line, files)
            if self.cache.is_cached(name, task_hash, project_dir):
                print(f"[SKIP] {name} (up to date)")
                return True
        else:
            task_hash = None

        # 5. Pre hooks
        self.plugins.run_hooks(f"Pre{name}", project_dir)

        # 6. Execute
        if self.dry_run:
            print(f"[DRY] {name}: {command_line}")
            return True

        print(f"[RUN]  {name}: {command_line}")
        result = self._execute(command_line, project_dir)

        # 7. Handle result
        if result.returncode != 0:
            self.plugins.run_hooks(f"OnFail{name}", project_dir)
            raise TaskFailedError(f"Task '{name}' failed (exit {result.returncode})")

        # 8. Post hooks + cache update
        self.plugins.run_hooks(f"Post{name}", project_dir)
        if task_hash is not None:
            self.cache.update(name, task_hash, outputs=output_files)

        return True

    @staticmethod
    def _execute(command_line, project_dir):
        """Run a command with correct shell handling per platform.

        On Windows, subprocess.run shell=True uses bash (from Git Bash)
        which breaks cmd.exe syntax.  We detect ``cmd /c`` prefixes,
        strip them, and route the remainder through cmd.exe with
        proper ``executable=`` to avoid bash interference.

        For non-cmd commands on Windows, we resolve relative
        executables (like ``main.exe``) to absolute paths and run
        them directly, or fall back to cmd.exe as the shell.
        """
        abs_dir = os.path.abspath(project_dir)

        # Detect and properly route `cmd /c` prefixed commands
        if command_line.lower().startswith("cmd /c ") or \
           command_line.lower().startswith("cmd.exe /c "):
            rest = re.sub(r'^cmd(?:\.exe)?\s+/c\s+', '', command_line,
                          flags=re.IGNORECASE)
            if os.name == "nt":
                # cmd.exe cannot find bare relative executables
                # like "main.exe" — it needs ".\main.exe" or a
                # full path.  Patch up the first word if needed.
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

        # For non-cmd commands on Windows
        if os.name == "nt":
            parts = command_line.split()
            if parts:
                exe = parts[0]
                # If it looks like a local executable (e.g. main.exe),
                # resolve it against the project directory so
                # subprocess can find it.
                if not os.path.isabs(exe) and \
                   not any(c in exe for c in '/\\') and \
                   exe.lower().endswith(('.exe', '.bat', '.cmd', '.com')):
                    abs_exe = os.path.join(abs_dir, exe)
                    if os.path.isfile(abs_exe):
                        return subprocess.run(
                            [abs_exe] + parts[1:], cwd=abs_dir,
                        )

            # Otherwise, use shell=True with COMSPEC.
            return subprocess.run(
                command_line, shell=True, cwd=abs_dir,
                executable=os.environ.get("COMSPEC", "cmd.exe"),
            )

        # Linux/macOS: shell=True works fine
        return subprocess.run(command_line, shell=True, cwd=abs_dir)

    @staticmethod
    def _extract_output_paths(output_str, project_dir):
        """Extract output file paths from the Output field.

        Looks for `-o <path>` patterns and bare file paths.
        Returns a list of paths relative to project_dir.
        """
        paths = []
        # Match -o <path> patterns
        for m in re.finditer(r'-o\s+(\S+)', output_str):
            paths.append(m.group(1))
        # Match -o<path> (no space)
        for m in re.finditer(r'-o(\S+)', output_str):
            paths.append(m.group(1))
        return paths


# ══════════════════════════════════════════════════════════════════
#  INI Helpers
# ══════════════════════════════════════════════════════════════════

def parse_ini(filepath):
    """Parse an INI file, returning a ConfigParser with case-sensitive keys."""
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"Missing file: {filepath}")
    config = configparser.ConfigParser()
    config.optionxform = str          # preserve case
    config.read(filepath, encoding="utf-8")
    return config


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


def load_mold(system_name, project_dir):
    """Load the mold file for the given system name."""
    if not system_name:
        return None
    mold_path = os.path.join(project_dir, f"{system_name.lower()}.ini")
    if os.path.exists(mold_path):
        return parse_ini(mold_path)
    print(f"[WARN] Mold file not found: {mold_path}")
    return None


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


# ══════════════════════════════════════════════════════════════════
#  Main — CLI entry point
# ══════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        prog="bloomery",
        description="Bloomery — A Metaprogrammable Build System",
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
    args = parser.parse_args()

    # Resolve project dir
    project_path = os.path.abspath(args.project)
    project_dir = os.path.dirname(project_path) or "."

    # Parse project config
    config = parse_ini(project_path)

    # List targets
    if args.list:
        list_targets(config)
        return

    # Build variable map:  [Variables] → profile → CLI
    variables = load_variables(config)

    # Apply profile variables
    if args.profile:
        profiles = load_profiles(config)
        if args.profile in profiles:
            variables.update(profiles[args.profile])
        else:
            print(f"[WARN] Unknown profile: {args.profile}")
            print(f"       Available: {', '.join(profiles.keys()) or '(none)'}")

    # Parse CLI variable overrides
    cli_vars = {}
    for d in args.D:
        if "=" in d:
            k, v = d.split("=", 1)
            cli_vars[k.strip()] = v.strip()
        else:
            cli_vars[d.strip()] = "true"

    # Load mold
    system_name = config.get("Meta", "System", fallback="")
    mold_config = load_mold(system_name, project_dir)

    # Set default output variable from task config
    if "output" not in variables and not cli_vars.get("output"):
        # Try to infer from the first build task
        for _alias, section_name in config.get("Tasks", {}).items():
            section_key = f"Tasks.{section_name}"
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

    # Apply mold defaults to variables (lower priority than existing vars)
    if mold_config and mold_config.has_section("Definitions"):
        for key, value in mold_config["Definitions"].items():
            if key not in ctx.variables:
                ctx.set_var(key, value)

    # Load plugins
    plugins = PluginManager(config, engine)
    plugins.load()

    # Build DAG
    dag = TaskDAG()
    dag.build(config)

    # Determine execution order
    if args.targets:
        # Map alias → section name
        alias_map = {k: v for k, v in config["Tasks"].items()}
        resolved_targets = []
        for t in args.targets:
            if t in alias_map:
                resolved_targets.append(alias_map[t])
            else:
                resolved_targets.append(t)           # assume it's a section name
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

    print(f"{'=' * 50}")
    print(f"  Bloomery  |  {config.get('Meta', 'Name', fallback='?')}")
    print(f"  Platform  |  {ctx.platform}")
    print(f"  Targets   |  {' -> '.join(order)}")
    print(f"{'=' * 50}\n")

    for task_name in order:
        section_key = f"Tasks.{task_name}"
        if not config.has_section(section_key):
            continue

        print(f"-- {task_name} --")
        task_config = dict(config.items(section_key))

        try:
            runner.run_task(task_name, task_config, project_dir)
        except TaskFailedError as e:
            print(f"\nX {e}")
            sys.exit(1)

        print()

    cache.save()
    print("OK - All tasks completed.")


if __name__ == "__main__":
    main()
