import hashlib
import json
import os

CACHE_FILE = ".bloomery_cache.json"


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
