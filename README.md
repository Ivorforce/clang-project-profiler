## Clang project profiler

An experimental profiler for cpp projects. It estimates two metrics:

- `compile-costs.txt`: The total compile time caused by any file. This includes partial responsibility over includees.
- `compile-costs-recompile.txt`: The capacity to cause recompilations by any file. This is estimated from its size, the compile time of includer compile units, and partial responsibility over the size of includees.

Partial responsibility is calculated as the total number of include paths to a file, divided by the number of include paths going through the current file.

## How to run

First, build the `include-tracker`:

```shell
CMAKE_PREFIX_PATH="/usr/local/opt/llvm" cmake .
make
```

Then, run the python script:

```shell
# To test it's running correctly.
python run.py --input <path/to/compile_commands.json> --limit 100
python run.py --input <path/to/compile_commands.json>
```

A complete run takes 2-3 times longer than a fresh compile of the project.
