#!/usr/bin/env python3
import io

import json
import subprocess
import os
import sys
import math
import pathlib
import shlex

import concurrent.futures
import multiprocessing
import dataclasses
import tempfile
import argparse
import re

arg_parser = argparse.ArgumentParser(
    description='What the program does',
    epilog='Text at the bottom of help')

arg_parser.add_argument('--input', required=False, default="compile_commands.json")
arg_parser.add_argument('--limit', required=False, default=-1, type=int)
arg_parser.add_argument('--sync', action='store_true')

@dataclasses.dataclass
class IncludeInfo:
    filename: str
    includees: list["IncludeInfo"]
    explicit_include_count: int

    deep_include_weights: dict["IncludeInfo", float] = dataclasses.field(default=dict)
    self_size: int = 0

    def visit_dfs(self, visited: set[str]):
        visited.add(self.filename)
        for includee in self.includees:
            if includee.filename not in visited:
                yield from includee.visit_dfs(visited)
        yield self

    def __eq__(self, other):
        return isinstance(other, IncludeInfo) and self.filename == other.filename

    def __hash__(self):
        return hash(self.filename)

@dataclasses.dataclass
class CompileUnitCosts:
    compile_cost_us: dict[str, float]
    recompile_cost_us: dict[str, float]

def include_graph_from_include_tracker(input_str: str) -> dict[str, IncludeInfo]:
    entries: dict[str, IncludeInfo] = dict()

    for line in input_str.strip().splitlines():
        if not line.startswith("# "):
            continue

        filename, includer_filename = line[2:].split(" | ")
        filename = str(pathlib.Path(filename).absolute())
        includer_filename = str(pathlib.Path(includer_filename).absolute())

        entry = entries.setdefault(filename, IncludeInfo(includees=[], filename=filename, explicit_include_count=0))
        includer_entry = entries.setdefault(includer_filename, IncludeInfo(includees=[], filename=includer_filename, explicit_include_count=0))

        assert (entry != includer_entry)

        entry.explicit_include_count += 1

        if all(i != entry for i in includer_entry.includees):
            # Might be included multiple times
            includer_entry.includees.append(entry)

    return entries

def self_times_from_time_trace_file(path, dir: pathlib.Path) -> dict[str, int]:
    # TODO Find compile unit self time somehow
    with pathlib.Path(path).open("r") as f:
        trace_dict = json.load(f)

    pid = trace_dict["traceEvents"][0]["pid"]
    tid = trace_dict["traceEvents"][0]["tid"]

    trace_events = trace_dict["traceEvents"]
    trace_events.sort(key=lambda e: e["ts"])

    self_times: dict[str, int] = dict()
    stack: list[tuple[str, int, int]] = []
    children_time: int = 0

    for entry in trace_events:
        if entry["name"] != "Source":
            continue

        assert entry["pid"] == pid
        assert entry["tid"] == tid

        timestamp = entry["ts"]

        if entry["ph"] == "b": # begin
            filename = str((dir / entry["args"]["detail"]).absolute())
            stack.append((filename, timestamp, children_time))
            children_time = 0
        elif entry["ph"] == "e": # end
            filename, start_timestamp, parent_children_time = stack.pop()
            self_times[filename] = (timestamp - start_timestamp) - children_time
            children_time = parent_children_time + (timestamp - start_timestamp)
        else:
            raise

    return self_times

def evaluate_compile_entry(idx: int, entry: dict) -> CompileUnitCosts:
    directory = pathlib.Path(entry.get("directory", os.getcwd()))
    command = entry.get("command")
    arguments = entry.get("arguments")

    # Prefer 'arguments' if present, otherwise split 'command'
    if arguments:
        cmd = arguments
    else:
        cmd = shlex.split(command)

    compile_unit_path = cmd[-1]

    if idx % 10 == 0:
        print(f"{idx:04d}", compile_unit_path)

    o_idx = cmd.index("-o")
    if not o_idx:
        raise

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_dir = pathlib.Path(tmp_dir)
        target_file = cmd[-1]
        cmd = cmd[:o_idx] + ["-Wno-everything"] + cmd[o_idx + 2:]

        tracker_path = pathlib.Path("cmake-build-debug/include-tracker").absolute()

        # TODO Check=false currently needed :/
        result = subprocess.run([tracker_path, *[f"--extra-arg={arg}" for arg in cmd[1:-1]], target_file], cwd=directory, check=False, capture_output=True, text=True)
        files_by_name: dict[str, IncludeInfo] = include_graph_from_include_tracker(result.stdout)

        trace_json_path = tmp_dir / "trace.json"
        subprocess.run(cmd + ["-o", tmp_dir / "out.o", "-ftime-trace=" + str(trace_json_path)], cwd=directory, check=True, capture_output=True)
        self_times = self_times_from_time_trace_file(trace_json_path, dir=directory)

    head_include_info = files_by_name[str(directory / compile_unit_path)]

    directory_str = str(directory)

    def is_internal(path: str):
        # Ignore generated code, it likely won't change often.
        return path.startswith(directory_str) and not ("thirdparty" in path) and not (".gen." in path)

    # Visit deep nodes first, so that the deep includees are available when the parent is evaluating.
    for include_info in head_include_info.visit_dfs(set()):
        if is_internal(include_info.filename):
            include_info.self_size = (directory / include_info.filename).stat().st_size

        include_info.deep_include_weights = {include_info: 0.0}

        for includee in include_info.includees:
            include_info.deep_include_weights.update(includee.deep_include_weights)

    assert (set(files_by_name.values()) == set(head_include_info.deep_include_weights))

    next = [head_include_info]
    head_include_info.deep_include_weights = {key: 1.0 for key in head_include_info.deep_include_weights}

    while next:
        include_info = next.pop()
        counts_by_includee: dict[IncludeInfo, int] = {filename: 0 for filename in include_info.deep_include_weights}

        for includee in include_info.includees:
            for deep_include in includee.deep_include_weights:
                counts_by_includee[deep_include] += 1

        for includee in include_info.includees:
            for deep_include in includee.deep_include_weights:
                # Add weights
                includee.deep_include_weights[deep_include] += include_info.deep_include_weights[deep_include] / counts_by_includee[deep_include]

            includee.explicit_include_count -= 1
            if includee.explicit_include_count == 0:
                next.append(includee)

    for include_info in files_by_name.values():
        if include_info.explicit_include_count != 0:
            # TODO Some system headers seem to be circular or something? But we don't need to be super precise with them.
            assert not is_internal(include_info.filename), include_info.filename
            include_info.deep_include_weights[include_info] = 1
            include_info.explicit_include_count = 0

    # print(list(include_info.filename for include_info in files_by_name.values() if include_info.explicit_include_count != 0))
    assert all(include_info.explicit_include_count == 0 for include_info in files_by_name.values())
    # print(list((include_info.filename, include_info.deep_include_weights[include_info]) for include_info in files_by_name.values()))
    assert all(math.isclose(include_info.deep_include_weights[include_info], 1) for include_info in files_by_name.values())

    # print(json.dumps(list(deep_includes[head_include_info.filename]), indent=2))

    # end - start would be a better estimate, but this is good enough
    compile_unit_total_compile_time: float = sum(self_times.values())

    deep_times: dict[str, int] = {
        include_info.filename: sum(
            self_times.get(includee.filename, 0) * weight
            for includee, weight in include_info.deep_include_weights.items()
        )
        for include_info in files_by_name.values()
    }
    if is_internal(head_include_info.filename):
        total_file_size: int = sum(include_info.self_size for include_info in files_by_name.values())
        deep_recompile_times: dict[str, float] = {
            include_info.filename: sum(
                includee.self_size * weight
                for includee, weight in include_info.deep_include_weights.items()
            ) / total_file_size * compile_unit_total_compile_time
            for include_info in files_by_name.values()
        }
        # + 1 to account for floating point inaccuracies
        assert (v <= compile_unit_total_compile_time + 1 for v in deep_recompile_times.values())
        assert (abs(deep_recompile_times[head_include_info.filename] - compile_unit_total_compile_time) < 1)
    else:
        # Non internal compile units never recompile (unless explicitly updated).
        deep_recompile_times: dict[str, float] = {}

    # print(json.dumps(self_sizes, indent=4))
    # print(json.dumps(deep_sizes, indent=4))
    # print(json.dumps(deep_recompile_times, indent=4))

    return CompileUnitCosts(compile_cost_us=deep_times, recompile_cost_us=deep_recompile_times)


def main():
    args = arg_parser.parse_args()
    input_filename = args.input
    limit = args.limit

    # Read the compile_commands.json file
    try:
        with open(input_filename, "r") as f:
            compile_commands = json.load(f)
    except Exception as e:
        print(f"Error reading {input_filename}: {e}")
        sys.exit(1)

    print(f"Starting {len(compile_commands)} commands...")

    if limit >= 0:
        compile_commands = compile_commands[:limit]

    total_compile_costs_us: dict[str, float] = {}
    total_recompile_costs_us: dict[str, float] = {}

    failure_count = 0

    def add_result(costs: CompileUnitCosts):
        for filename, cost in costs.compile_cost_us.items():
            total_compile_costs_us.setdefault(filename, 0)
            total_compile_costs_us[filename] += cost
        for filename, cost in costs.recompile_cost_us.items():
            total_recompile_costs_us.setdefault(filename, 0)
            total_recompile_costs_us[filename] += cost

    if args.sync:
        for (i, cmd) in enumerate(compile_commands):
            try:
                add_result(evaluate_compile_entry(i, cmd))
            except Exception as e:
                directory = pathlib.Path(cmd.get("directory", os.getcwd()))
                print(f"Command {i} failed with {type(e)}:")
                print(f"cd \"{directory}\" && " + cmd["command"])
                failure_count += 1
    else:
        executor = concurrent.futures.ProcessPoolExecutor(multiprocessing.cpu_count())
        futures = [executor.submit(evaluate_compile_entry, *item) for item in enumerate(compile_commands)]
        completed, not_completed = concurrent.futures.wait(futures)

        print(f"Fetching results...")

        for future in completed:
            try:
                add_result(future.result())
            except:
                failure_count += 1

    print(f"Combining results...")

    def costs_to_string(costs: dict[str, float], unit: str):
        cost_list = list(kv for kv in costs.items() if kv[1] // 1000 // 1000 > 0)
        cost_list.sort(key=lambda kv: kv[1], reverse=True)
        return "\n".join(f"{kv[0]}: {int(kv[1] // 1000 // 1000)}{unit}" for kv in cost_list)

    result_path = pathlib.Path("./compile-costs.txt")
    result_path.write_text(costs_to_string(total_compile_costs_us, unit="s"))
    result_path_internal = pathlib.Path("./compile-costs-recompile.txt")
    result_path_internal.write_text(costs_to_string(total_recompile_costs_us, unit="s"))

    print(f"Done. {failure_count} object files failed to analyze.")

    print(result_path.absolute())
    print(result_path_internal.absolute())

if __name__ == "__main__":
    main()
