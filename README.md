# Bloomery — A Metaprogrammable Build System
<small>Partially AI generated</small><br><br>
Bloomery is a declarative, INI-based build system with a powerful metaprogramming DSL. Configure builds using simple `.ini` files, leverage reusable **molds** (templates), and express complex build logic with directives like conditionals, loops, shell substitution, and variable interpolation.

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

Requires Python 3.8+ and has no runtime dependencies.

## Quick Start

```bash
# Build and run
bloomery project.ini

# Run a specific target
bloomery project.ini run

# Force a full rebuild
bloomery project.ini --clean

# Dry run (show commands, don't execute)
bloomery project.ini --dry-run

# Override a variable
bloomery project.ini -D debug=false

# List available targets
bloomery project.ini --list

# Verbose mode (show directive resolution)
bloomery project.ini --verbose

# Use a build profile
bloomery project.ini --profile release

# Build independent tasks in parallel (0 = one job per CPU)
bloomery project.ini -j4

# Print the version
bloomery --version
```

## Project File Structure

```
project/
├── project.ini           ← Your project build config
├── main.cpp              ← Source files
├── c++.ini               ← Optional: a local mold overriding the bundled one
└── .bloomery_cache.json  ← Auto-generated build cache
```

A runnable example lives in [`examples/hello-cpp/`](examples/hello-cpp/):

```bash
cd examples/hello-cpp
bloomery project.ini
```

## Configuration Reference

### Project INI (`project.ini`)

```ini
[Meta]
Name = myproject
Version = 1.0
Author = You
System = c++              ; Which mold to load

[Variables]
compiler = g++            ; Project-level variables
debug = true
output = myapp.exe

[Plugins.Command]
Prefix = <if(platform=windows)>cmd /c<end>

[Plugins.Hooks]
PostBuild = echo Done!         ; Pre<Task> / Post<Task> / OnFail<Task>

[Tasks]
build = Build             ; alias = SectionName
run = Run
clean = Clean

[Tasks.Build]
Depends =                  ; Comma-separated task dependencies
Command = {compiler}
Flags = -std=c++17 <if(var(debug))>-g -O0<else>-O2<end>
Files = <files ending (.cpp|.cc|.cxx|.c)>
Exclude = *_test.cpp       ; Glob patterns to exclude (optional)
Output = -o {output}

[Tasks.Run]
Depends = Build
Command = {output}

[Tasks.Clean]
Command = <if(platform=windows)>cmd /c del /q<else>rm -f<end>
Flags = {output}
```

### Mold INI (`c++.ini`)

Molds provide reusable build presets. A project references a mold via `Meta.System`.

```ini
[Bloomery]
Mold = c++

[Meta]
Name = C++
Version = 0.1

[Definitions]              ; Default values that tasks can inherit
Compiler = g++
Standard = 17
Flags = -std=c++<var(standard)>
Files = <files ending (.cpp|.cc|.cxx|.c)>
Output = -o <input>.exe
```

### Profiles (`[Profiles.X]`)

Profiles let you define variable sets for different build modes:

```ini
[Profiles.debug]
debug = true
optimize = false

[Profiles.release]
debug = false
optimize = true
```

Activate with `--profile debug` or `--profile release`.

## Metaprogramming DSL

### Variables

```ini
[Variables]
myvar = hello

; Directives
Flags = <var(myvar)>                      → hello
Command = <env(PATH)>                     → system PATH value
Output = -o <shell(date +%Y%m%d)>.exe    → -o 20260620.exe
```

### Interpolation

```ini
Flags = {compiler} -Wall                 → g++ -Wall
Name = {env.USER}_build                  → alice_build
```

### Conditional — `<if>`

```ini
; Platform condition
Command = <if(platform=windows)>g++.exe<else>g++<end>

; Variable truthiness
Flags = <if(var(debug))>-g -O0<else>-O2<end>

; Variable equality
Flags = <if(var(mode)=safe)>-fsanitize=address<end>

; Negation
Flags = <if(!var(debug))>-DNDEBUG<end>

; File existence
Flags = <if(exists(config.h))>-DHAVE_CONFIG<end>

; Environment variable
Flags = <if(env(CI))>-DCI_BUILD<end>

; elif chain
Flags = <if(var(level)=1)>-O1<elif(var(level)=2)>-O2<elif(var(level)=3)>-O3<else>-O0<end>
```

### Loops — `<for>`

```ini
; Literal list
Defines = <for(d in DEBUG|VERBOSE)>-D{d}<end>     → -DDEBUG -DVERBOSE

; File iteration
Check = <for(f in files: .cpp)>echo {f}<end>

; Numeric range
Ids = <for(i in range(1,5))>-DID_{i}<end>         → -DID_1 -DID_2 -DID_3 -DID_4
```

### File Resolution

```ini
; By extension (in project directory)
Files = <files ending (.cpp|.cc|.cxx|.c)>

; By glob pattern (recursive)
Files = <files matching (src/**.cpp)>

; Directory-scoped + extension
Files = <files in (lib; .cpp|.cc)>

; Exclude patterns (separate field)
Exclude = *_test.cpp *_bench.cpp
```

## Incremental Builds

### Header dependencies

Bloomery hashes file contents to decide what to rebuild. Files that the
compiler reads via `#include` never appear on the command line, so they
must be declared — otherwise editing a header leaves you with a stale
binary. There are two ways:

```ini
; Explicit: hashed, but never passed to the compiler
Headers = <files ending (.h|.hpp)>

; Automatic: read the compiler's own dependency output
Flags   = -MMD -MF obj/<stem>.d
DepFile = obj/<stem>.d
```

`DepFile` is preferred — it tracks exactly the headers each file actually
included, transitively, instead of over-invalidating on every header.

### Per-file compilation

`Mode = per-file` runs one command per input with its own cache entry, so
editing one source recompiles only that source. `<stem>` is the current
file's name without its extension, and `{task.X.outputs}` lets a link step
consume what a compile step produced:

```ini
[Tasks.Compile]
Mode = per-file
Command = {compiler}
Flags = -c -MMD -MF obj/<stem>.d
Files = <files ending (.cpp)>
Output = -o obj/<stem>.o
DepFile = obj/<stem>.d

[Tasks.Link]
Depends = Compile
Command = {compiler}
Files = {task.Compile.outputs}
Output = -o {output}
```

```
$ bloomery project.ini            # after editing only main.cpp
[RUN]  Compile [main.cpp]: ...
[SKIP] Compile [util.cpp] (up to date)
[RUN]  Link: ...
```

Per-file tasks are also what make `-j` worthwhile — independent tasks in
the same dependency level run concurrently.

## Molds

Molds are looked up by `Meta.System` in this order, first match winning:

1. The project directory
2. `Meta.MoldPath` (paths relative to the project)
3. `$BLOOMERY_MOLD_PATH`
4. `~/.bloomery/molds/`
5. The molds bundled with Bloomery — currently `c`, `c++`, `rust`, `python`

So `System = c` works with no local file, and dropping a `c.ini` beside
your project overrides the bundled one. A mold that can't be found is an
error listing every path searched.

Molds may build on one another:

```ini
[Bloomery]
Mold = c++
Extends = c        ; inherit any [Definitions] not defined here
```

Mold definitions are reached with `<mold(Field)>` or `{mold.Field}`; they
are *not* copied into the variable namespace, so a mold key named `Files`
cannot shadow a project variable of the same name.

### Mold References

```ini
; Use mold's default for the current field
Flags = <mold()> -Wall

; Use mold's specific field
Command = <mold(Compiler)>

; Explicitly no mold contribution (empty)
Files = <mold(none)> <files matching (vendor/**.c)>
```

### Special Directives

| Directive | Description |
|---|---|
| `<platform>` | Current OS: `windows`, `linux`, or `macos` |
| `<exists(path)>` | `'true'` if file exists, else `'false'` |
| `<input>` | Primary source file stem (auto-set from Files) |
| `<stem>` | Current file's stem, in a `Mode = per-file` task |
| `<shell(cmd)>` | Captured stdout of shell command |

## Architecture

```
bloomery/
├── core.py              — Everything below
├── molds/               — Bundled build presets (c, c++, rust, python)
│
├── Context              — Evaluation state (variables, platform, mold)
├── DirectiveEngine      — Metaprogramming template resolver
│   ├── resolve()        — Main entry point
│   ├── Control flow      — <if>/<elif>/<else>/<end>, <for>/<end>
│   ├── Simple directives — <var>, <env>, <shell>, <mold>, <files…>
│   ├── Interpolation     — {expr}
│   └── Condition eval     — Platform, var, env, exists checks
├── BuildCache           — Content-based incremental builds + header deps
├── TaskDAG              — Dependency graph, topological sort, parallel waves
├── PluginManager        — Command prefix, hooks
└── TaskRunner           — Field resolution → cache → execute
```

## Comparison with Make/CMake

| Feature | Bloomery | Make | CMake |
|---|---|---|---|
| Config format | INI | Makefile | CMakeLists.txt |
| Metaprogramming | Built-in DSL | Shell commands | CMake language |
| Templates/Molds | ✅ `.ini` molds | ❌ | ✅ Toolchain files |
| Conditional builds | `<if(platform=…)>` | `ifeq` | `if()` |
| Loops | `<for(var in list)>` | `foreach` | `foreach` |
| Incremental builds | Content-hash cache | Mtime checks | CMake cache |
| Header tracking | `DepFile` / `Headers` | `-MMD` + include | ✅ Automatic |
| Parallel builds | `-j N` | `-j N` | `--parallel N` |
| Cross-platform | `<platform>` + `<if>` | ❌ (manual) | ✅ Built-in |
| Learn curve | INI + directives | Makefile syntax | CMake language |

## Extending Bloomery

### Custom Directive Handlers

```python
from bloomery import DirectiveEngine, Context

ctx = Context(variables={"lib": "mylib"})
engine = DirectiveEngine(ctx)

def handle_lib(args):
    return f"-l{args or ctx.get_var('lib')}"

engine.register_handler("lib", handle_lib)
result = engine.resolve("<lib(boost)>")   # → "-lboost"
```

### Custom Hooks

```ini
[Plugins.Hooks]
PreBuild = echo "Starting build..."
PostBuild = echo "Build complete!"
OnFailBuild = echo "Build failed!" && exit 1
```

## Development

```bash
pip install -e ".[dev]"
pytest
```

## Changelog

See [CHANGELOG.md](CHANGELOG.md).

## License

MIT — see [LICENSE](LICENSE).
