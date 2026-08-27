import platform as platform_module


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
