from collections import defaultdict, deque

from bloomery.errors import CyclicDependencyError

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
        # ponytail
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
