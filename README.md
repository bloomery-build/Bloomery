# Bloomery — A TOML-native Build System
<small>Partially AI generated README content</small><br><br>
Bloomery is a declarative build system driven entirely by plain TOML. There
is no embedded scripting language: conditionals are ordinary TOML tables
with a reserved `on` key, file resolution and other operations are tables
with a reserved verb key, and everything else is `{name}`-style
interpolation. Reusable **molds** (build presets) give you sane defaults
per language.

## Installation

```bash
pip install bloomery-build
```

The distribution is `bloomery-build` (`bloomery` was already taken on PyPI),
but this installs the `bloomery` command itself — the examples below use
that. Installing from a local checkout works the same way: `pip install .`
If you'd rather not install anything, `python -m bloomery` works identically
everywhere.

To track the git checkout instead, so updates are just a pull:

```bash
bloomery install     # pip install -e the checkout (clones one if needed)
bloomery update      # git pull it
bloomery uninstall   # pip uninstall bloomery-build
```

New to a project? `bloomery init` scaffolds a starter `bloomery.toml` in
the current directory — asks for a project name and (optionally) a mold
name, then writes `[meta]`/`[variables]`/`[plugins.command]` with sane
defaults for you to add `[tasks.*]` to:

```bash
bloomery init
```

Requires Python 3.8+. Uses the stdlib `tomllib` on 3.11+; on 3.8–3.10 it
pulls in `tomli`, a small pure-Python TOML parser — the only dependency
Bloomery has, and only there because `tomllib` hasn't shipped yet.

## Quick Start

```bash
# Build and run
bloomery project.toml

# Run a specific target
bloomery project.toml run

# Force a full rebuild
bloomery project.toml --clean

# Dry run (show commands, don't execute)
bloomery project.toml --dry-run

# Override a variable
bloomery project.toml -D debug=false

# List available targets
bloomery project.toml --list

# Use a build profile
bloomery project.toml --profile release

# Build independent tasks in parallel (0 = one job per CPU)
bloomery project.toml -j4

# Print the version
bloomery --version
```

## Project File Structure

```
project/
├── project.toml           ← Your project build config
├── main.cpp                ← Source files
├── c++.toml                ← Optional: a local mold overriding the bundled one
└── .bloomery_cache.json     ← Auto-generated build cache
```

A runnable example lives in [`examples/hello-cpp/`](examples/hello-cpp/):

```bash
cd examples/hello-cpp
bloomery project.toml
```

## Configuration Reference

### Project TOML (`project.toml`)

```toml
[meta]
name = "myproject"
version = "1.0"
system = "c++"                # which bundled mold to load

[variables]
compiler = "g++"
debug = true
output = "myapp.exe"

[plugins.command]
prefix = { on = "platform", windows = "cmd /c", default = "" }

[plugins.hooks.build]
post = "echo Done!"            # scoped under [plugins.hooks.<task>]

[variables]
debug_flags = { on = "debug", true = "-g -O0", false = "-O2" }

[tasks.build]
depends = []                    # task names this one depends on
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

A dispatch table has to be the *entire* value of a field — it can't sit
inside a longer string the way you might expect from a templating language.
When a field needs to mix literal text with a dispatch, as `flags` does
above, give the dispatch its own named variable and reference it with
`{debug_flags}`; there's no way to write the dispatch inline in `flags`
itself.

Task names are the `[tasks.*]` keys themselves — `bloomery project.toml
build` looks up `[tasks.build]` directly. There's no separate alias table;
TOML's own key naming does that job.

### Mold TOML (`c++.toml`)

Molds provide reusable build presets. A project references one via `meta.system`.

```toml
[bloomery]
mold = "c++"

[meta]
name = "C++"
version = "0.1"

[definitions]
compiler = "g++"
standard = "17"
flags = "-std=c++{mold.standard}"
files = { ending = [".cpp", ".cc", ".cxx", ".c"] }
output = "-o {input}.exe"
```

### Profiles (`[profiles.X]`)

Profiles overlay a named set of variables on top of `[variables]`:

```toml
[profiles.debug]
debug = true
debug_flags = "-g -O0"

[profiles.release]
debug = false
debug_flags = "-O2 -DNDEBUG"
```

Activate with `--profile debug` or `--profile release`. Profiles are the
right tool for "this value differs by build mode" — reach for a dispatch
table only for things that aren't a mode choice, like platform, which
nothing selects and Bloomery just observes.

## The DSL: interpolation + dispatch tables

Every project/mold file is plain TOML — a standard TOML parser reads it
correctly with zero knowledge of Bloomery. Two conventions carry all of
what would be "metaprogramming" in another build system.

### Interpolation

Any string value may contain `{...}` references:

```toml
flags = "{compiler} -Wall"                → g++ -Wall
name  = "{env.USER}_build"                → alice_build
flags = "{mold.flags}"                    → mold's own flags field
output = "{task.compile.outputs}"         → files another task produced
```

### Dispatch tables — the `on` key

A table with an `on` key picks a branch by the current value of that
variable, with `default` as the fallback:

```toml
output = { on = "platform", windows = "hello.exe", default = "hello" }
flags  = { on = "debug", true = "-g -O0", false = "-O2 -DNDEBUG" }
```

A branch can itself be another dispatch table, a list, or a plain string —
resolution recurses. `-D debug=false` on the command line, or an override
from `[profiles.X]`, both just change what a dispatch reads; nothing about
the mechanism cares where the value came from.

### Reserved-key tables — files, shell, exists

A handful of other reserved keys cover the rest of what used to be tag
directives:

```toml
files = { ending = [".cpp", ".cc"] }             # by extension, project dir
files = { in = "lib", ending = [".cpp"] }        # ...scoped to a directory
files = { matching = "src/**.cpp" }              # by glob, recursive
greeting = { shell = "date +%Y%m%d" }            # captured stdout
has_cfg  = { exists = "config.h" }               # "true" / "false"
defines  = { prefix = "-D", items = ["A", "B"] } # → "-DA -DB"
```

Every value here is resolved recursively too, so `items` can itself be a
dispatch table, another reserved-key table, or an interpolated string.

## Incremental Builds

### Header dependencies

Bloomery hashes file contents to decide what to rebuild. Files the
compiler reads via `#include` never appear on the command line, so they
must be declared — otherwise editing a header leaves you with a stale
binary. There are two ways:

```toml
headers = { ending = [".h", ".hpp"] }     # explicit: hashed, never passed to the compiler

flags   = "-c -MMD -MF obj/{stem}.d"      # automatic: read the compiler's own
depfile = "obj/{stem}.d"                  # dependency output
```

`depfile` is preferred — it tracks exactly the headers each file actually
included, transitively, instead of over-invalidating on every header.

### Per-file compilation

`mode = "per-file"` runs one command per input with its own cache entry, so
editing one source recompiles only that source. `{stem}` is the current
file's name without its extension, and `{task.X.outputs}` lets a link step
consume what a compile step produced:

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

```
$ bloomery project.toml            # after editing only main.cpp
[RUN]  compile [main.cpp]: ...
[SKIP] compile [util.cpp] (up to date)
[RUN]  link: ...
```

Per-file tasks are also what make `-j` worthwhile — independent tasks in
the same dependency level run concurrently.

## Molds

Molds are looked up by `meta.system` in this order, first match winning:

1. The project directory
2. `meta.mold_path` (paths relative to the project)
3. `$BLOOMERY_MOLD_PATH`
4. `~/.bloomery/molds/`
5. The molds bundled with Bloomery — currently `c`, `c++`, `rust`, `python`

So `system = "c"` works with no local file, and dropping a `c.toml` beside
your project overrides the bundled one. A mold that can't be found is an
error listing every path searched.

Molds may build on one another:

```toml
[bloomery]
mold = "c++"
extends = "c"       # inherit any [definitions] not defined here
```

Mold definitions are reached with `{mold.field}` only; they are *not*
copied into the variable namespace, so a mold key named `files` cannot
shadow a project variable of the same name. A mold's own definitions
cross-reference each other the same way — `flags = "-std=c++{mold.standard}"`
inside the mold's own `[definitions]`.

### Special variables

| Name | Description |
|---|---|
| `{platform}` | Current OS: `windows`, `linux`, or `macos` — seeded automatically |
| `{input}` | Primary source file's stem (auto-set from `files`) |
| `{stem}` | Current file's stem, in a `mode = "per-file"` task |

These are ordinary variables, not special syntax — `{platform}` and a
dispatch `{ on = "platform", ... }` both just read the same value.

## Architecture

```
bloomery/
├── context.py     — Context: raw variables, platform, mold, per-task fork
├── evaluator.py   — Evaluator: dispatch tables, reserved keys, {interp}
├── cache.py       — BuildCache + depfile parsing
├── dag.py         — TaskDAG: dependency graph, topo sort, parallel waves
├── plugins.py     — PluginManager: command prefix, hooks
├── runner.py      — TaskRunner: field resolution → cache → execute
├── config.py      — parse_toml, profiles, mold search/inheritance
├── cli.py         — argparse, main(), task execution loop
├── selfmanage.py  — install/update/uninstall/init, the cli() entry point
├── errors.py      — BloomeryError and its subclasses
├── molds/         — Bundled build presets (c, c++, rust, python)
└── storage/       — bloomery init's starter template
```

Each module is a few hundred lines with a single job; `__init__.py`
re-exports the public API so `from bloomery import Evaluator` still works
regardless of which file it actually lives in.

There is no parser to speak of — `tomllib` does that — and no multi-pass
resolution loop. A value is walked once, recursively; a dispatch branch or
reserved-key argument that's itself a table is just another recursive call,
not more text to re-scan.

## Comparison with Make/CMake

| Feature | Bloomery | Make | CMake |
|---|---|---|---|
| Config format | TOML | Makefile | CMakeLists.txt |
| Metaprogramming | None — dispatch tables + interpolation | Shell commands | CMake language |
| Templates/Molds | ✅ `.toml` molds | ❌ | ✅ Toolchain files |
| Conditional builds | `{ on = "var", ... }` | `ifeq` | `if()` |
| Incremental builds | Content-hash cache | Mtime checks | CMake cache |
| Header tracking | `depfile` / `headers` | `-MMD` + include | ✅ Automatic |
| Parallel builds | `-j N` | `-j N` | `--parallel N` |
| Cross-platform | `{platform}` seeded automatically | ❌ (manual) | ✅ Built-in |
| Learn curve | TOML + two reserved-key conventions | Makefile syntax | CMake language |

## Development

```bash
pip install -e ".[dev]"
pytest
```

## Changelog

See [CHANGELOG.md](CHANGELOG.md).

## License

MIT — see [LICENSE](LICENSE).
