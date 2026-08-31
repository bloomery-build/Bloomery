import argparse
import os
import re
from concurrent.futures import ThreadPoolExecutor

from bloomery._version import __version__
from bloomery.cache import BuildCache
from bloomery.charges import ensure_installed
from bloomery.config import list_targets, load_mold, load_profiles, load_variables, parse_toml
from bloomery.context import Context
from bloomery.dag import TaskDAG
from bloomery.errors import TaskFailedError, UnknownTargetError
from bloomery.evaluator import Evaluator
from bloomery.plugins import PluginManager
from bloomery.runner import TaskRunner


def main():
    parser = argparse.ArgumentParser(
        prog="bloomery",
        description="A TOML-native Build System",
        epilog="Management: bloomery install | update | uninstall",
    )
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
    parser.add_argument("--manifest", default="bloomery.toml",
                        help="Path to the manifest file")
    parser.add_argument("--version", action="version",
                        version=f"bloomery {__version__}")
    args = parser.parse_args()

    project_path = os.path.abspath(args.manifest)
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

    # no-op unless the manifest opts in with [charges] auto_install = true
    ensure_installed(config, project_dir)

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
