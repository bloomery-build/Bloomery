import glob
import os
import re
import subprocess

from bloomery.context import Context
from bloomery.errors import BloomeryError


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
