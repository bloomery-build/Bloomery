# Bloomery — A TOML-native Build System
<small>Partially AI generated README content</small><br><br>

Bloomery is a build system configured entirely in TOML. There is no
embedded scripting language. Conditionals are TOML tables with a reserved
`on` key; file resolution and a few other operations are tables with a
reserved verb key; everything else is `{name}`-style interpolation.
Reusable **molds** provide default compiler settings per language.

## Installation

```bash
pip install bloomery-build
```

The distribution name is `bloomery-build` (`bloomery` was already taken on
PyPI); it installs a `bloomery` command. `pip install .` installs from a
local checkout the same way. `python -m bloomery` works without installing
at all.

```bash
bloomery install     # pip install -e a git checkout (clones one if needed)
bloomery update      # git pull that checkout, or pip upgrade if not a checkout
bloomery uninstall    # pip uninstall
bloomery init         # write a starter bloomery.toml in the current directory
```

Requires Python 3.8+. Uses `tomllib` on 3.11+; on 3.8–3.10 it installs
`tomli`, which is the package's only dependency.

## Quick Start

```bash
bloomery                            # build and run the default targets
bloomery run                        # run a specific target
bloomery --clean                    # ignore the cache, rebuild everything
bloomery --dry-run                  # print commands without running them
bloomery -D debug=false             # override a variable
bloomery --list                     # list targets
bloomery --profile release          # activate a profile
bloomery -j4                        # run independent tasks in parallel
bloomery --manifest project.toml    # load a manifest other than 
bloomery --version
```

## Project layout

```
project/
├── bloomery.toml           project build config
├── main.cpp                source files
├── c++.toml                 optional: a local mold, overrides the bundled one
└── .bloomery_cache.json      generated build cache
```

A working example is in [`examples/hello-cpp/`](examples/hello-cpp/):

```bash
cd examples/hello-cpp
bloomery
```

## Configuration reference

```toml
[meta]
name = "myproject"
version = "1.0"
system = "c++"                # bundled mold to load

[variables]
compiler = "g++"
debug = true
output = "myapp.exe"
debug_flags = { on = "debug", true = "-g -O0", false = "-O2" }

[plugins.command]
prefix = { on = "platform", windows = "cmd /c", default = "" }

[plugins.hooks.build]
post = "echo Done!"             # hooks are scoped under [plugins.hooks.<task>]

[tasks.build]
depends = []
command = "{compiler}"
flags = "-std=c++17 {debug_flags}"
files = { ending = [".cpp", ".cc", ".cxx", ".c"] }
exclude = ["*_test.cpp"]
output = "-o {output}"

[tasks.run]
depends = ["build"]
command = "{output}"

[tasks.clean]
command = { on = "platform", windows = "cmd /c del /q", default = "rm -f" }
flags = "{output}"
```

A dispatch table must be the entire value of a field. It cannot be
embedded inside a longer string. If a field needs literal text plus a
dispatch, as `flags` does above, put the dispatch in its own variable and
reference that variable.

A task's `[tasks.*]` key is also its invocation name — `bloomery
project.toml build` runs `[tasks.build]` directly. There is no separate
alias table.

### Interpolation

Any string may contain `{...}` references:

```toml
flags        = "{compiler} -Wall"
name         = "{env.USER}_build"
mold_flags   = "{mold.flags}"              # a field from the loaded mold
link_inputs  = "{task.compile.outputs}"    # output paths from another task
```

### Dispatch tables

A table with an `on` key selects a branch by the current value of that
variable, with `default` as the fallback. A branch may itself be another
dispatch table, a list, or a string; resolution is recursive.

```toml
output = { on = "platform", windows = "hello.exe", default = "hello" }
```

`-D` on the command line and `[profiles.X]` both work by changing the
variable a dispatch reads — neither is special-cased by the dispatch
mechanism itself.

### Reserved-key tables

```toml
cpp_files = { ending = [".cpp", ".cc"] }            # by extension, project dir
lib_files = { in = "lib", ending = [".cpp"] }       # by extension, in a directory
src_files = { matching = "src/**.cpp" }             # by glob, recursive
greeting  = { shell = "date +%Y%m%d" }              # captured stdout
has_cfg   = { exists = "config.h" }                 # "true" or "false"
defines   = { prefix = "-D", items = ["A", "B"] }   # -DA -DB
```

Values inside these tables are resolved the same way as anywhere else, so
`items` can be a dispatch table or another reserved-key table.

## Incremental builds

**Headers.** Bloomery hashes file contents to decide what to rebuild.
Headers included via `#include` do not appear on the command line, so
they must be declared or an edit to one won't trigger a rebuild.

```toml
headers = { ending = [".h", ".hpp"] }     # hashed, not passed to the compiler
flags   = "-c -MMD -MF obj/{stem}.d"      # or: read the compiler's own dependency output
depfile = "obj/{stem}.d"
```

`depfile` is more precise — it tracks only the headers a file actually
included, rather than invalidating on any header change.

**Output tracking.** If a task's recorded output file is missing, Bloomery
rebuilds it even if nothing else changed. For `-o <path>` this is detected
automatically. Compilers that use a different output flag (MSVC's `/Fe:`,
its linker's `/OUT:`, and others) need the path declared explicitly:

```toml
output  = "/Fe:app.exe"
outputs = ["app.exe"]
```

`outputs` is resolved like `files`/`headers` and can use `{stem}` or a
dispatch table.

**Per-file mode.** `mode = "per-file"` runs one command per input file,
each with its own cache entry, so editing one source only recompiles that
source. `{stem}` is the current file's name without extension.

```toml
[tasks.compile]
mode = "per-file"
command = "{compiler}"
flags = "-c -MMD -MF obj/{stem}.d"
files = { ending = [".cpp"] }
output = "-o obj/{stem}.o"
depfile = "obj/{stem}.d"

[tasks.link]
depends = ["compile"]
command = "{compiler}"
files = "{task.compile.outputs}"
output = "-o {output}"
```

Per-file tasks are also what `-j` parallelizes — independent tasks at the
same dependency depth run concurrently.

## Molds

A mold is looked up by `meta.system`, in this order:

1. The project directory
2. `meta.mold_path` (relative to the project)
3. `$BLOOMERY_MOLD_PATH`
4. `~/.bloomery/molds/`
5. Bundled molds — `c`, `c++`, `rust`, `python`

A missing mold is an error listing every path that was checked.

```toml
[bloomery]
mold = "c++"
extends = "c"       # inherit any [definitions] not set here

[definitions]
compiler = "g++"
standard = "17"
flags = "-std=c++{mold.standard}"
```

Mold fields are reached with `{mold.field}` and are not copied into the
project's variable namespace — a mold field named `files` will not shadow
a project variable named `files`. A mold's own definitions reference each
other the same way, as shown with `standard` above.

### Seeded variables

| Name | Value |
|---|---|
| `{platform}` | `windows`, `linux`, or `macos`, set automatically |
| `{input}` | stem of the first file in `files` |
| `{stem}` | current file's stem, in a `mode = "per-file"` task |

These are ordinary variables. `{platform}` and `{ on = "platform", ... }`
read the same value.

## Architecture

```
bloomery/
├── context.py      Context: variables, platform, mold, per-task fork
├── evaluator.py    Evaluator: dispatch tables, reserved keys, interpolation
├── cache.py        BuildCache, depfile parsing
├── dag.py          TaskDAG: dependency graph, topo sort, parallel waves
├── plugins.py      PluginManager: command prefix, hooks
├── runner.py       TaskRunner: resolves fields, checks cache, executes
├── config.py       parse_toml, profiles, mold search and inheritance
├── cli.py          argparse, main(), task execution loop
├── selfmanage.py   install/update/uninstall/init, cli() entry point
├── errors.py       BloomeryError and subclasses
├── molds/          bundled presets
└── storage/        bloomery init's template
```

`__init__.py` re-exports the public API, so `from bloomery import
Evaluator` works regardless of which module it's defined in.

## Development

```bash
pip install -e ".[dev]"
pytest
```

## Changelog

See [CHANGELOG.md](CHANGELOG.md).

## License

MIT — see [LICENSE](LICENSE).
