import fnmatch
import os
import re
import subprocess
import sys
import threading

from bloomery.cache import parse_depfile
from bloomery.errors import TaskFailedError


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
