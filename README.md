## Clang project profiler

An experimental profiler for cpp projects. It estimates two metrics:

- `compile-costs.txt`: The total compile time caused by any file. This includes partial responsibility over includees.
- `compile-costs-recompile.txt`: The capacity to cause recompilations by any file. This is estimated from its size, the compile time of includer compile units, and partial responsibility over the size of includees.

Partial responsibility is calculated by running down the include tree top-down, and splitting weights over individual deep includees across all headers that are explicitly included at this level.

## How to run

First, build the `include-tracker`:

```shell
CMAKE_PREFIX_PATH="/usr/local/opt/llvm" cmake .
make
```

Then, run the python script:

```shell
# To test it's running correctly.
python run.py --input <path/to/compile_commands.json> --try_one
# To test it's producing statistics as expected.
python run.py --input <path/to/compile_commands.json> --limit 100
python run.py --input <path/to/compile_commands.json>
```

A complete run takes about 1.5-2x times longer than a fresh compile of the project.
