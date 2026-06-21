# Bloomery — A Metaprogrammable Build System

Bloomery is a declarative, INI-based build system with a powerful metaprogramming DSL. Configure builds using simple `.ini` files, leverage reusable **molds** (templates), and express complex build logic with directives like conditionals, loops, shell substitution, and variable interpolation.

## Quick Start

```bash
# Build and run
python bloomery.py test.ini

# Run a specific target
python bloomery.py test.ini run

# Force a full rebuild
python bloomery.py test.ini --clean

# Dry run (show commands, don't execute)
python bloomery.py test.ini --dry-run

# Override a variable
python bloomery.py test.ini -D debug=false

# List available targets
python bloomery.py test.ini --list

# Verbose mode (show directive resolution)
python bloomery.py test.ini --verbose

# Use a build profile
python bloomery.py test.ini --profile release
```

## Project File Structure

```
project/
├── project.ini      ← Your project build config
├── c++.ini          ← Mold (reusable build preset)
├── main.cpp         ← Source files
└── .bloomery_cache.json  ← Auto-generated build cache
```

## Configuration Reference

### Project INI (`test.ini`)

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
OnBuildSuccess = echo Done!

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
Command = cmd /c del /q
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
Name = {env.USER}_build                   → k6303_build
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
| `<shell(cmd)>` | Captured stdout of shell command |

## Architecture

```
bloomery.py
├── Context              — Evaluation state (variables, platform, mold)
├── DirectiveEngine      — Metaprogramming template resolver
│   ├── resolve()        — Main entry point
│   ├── Control flow      — <if>/<elif>/<else>/<end>, <for>/<end>
│   ├── Simple directives — <var>, <env>, <shell>, <mold>, <files…>
│   ├── Interpolation     — {expr}
│   └── Condition eval     — Platform, var, env, exists checks
├── BuildCache           — Content-based incremental builds
├── TaskDAG              — Dependency graph with topological sort
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

## License

MIT
