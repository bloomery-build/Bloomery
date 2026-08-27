import subprocess
from collections import defaultdict


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
