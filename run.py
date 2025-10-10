#!/usr/bin/env python3
import io

import json
import subprocess
import os
import sys
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

@dataclasses.dataclass
class IncludeInfo:
    filename: str
    includees: list["IncludeInfo"]

    def visit_dfs(self, visited: set[str]):
        visited.add(self.filename)
        for includee in self.includees:
            if includee.filename not in visited:
                yield from includee.visit_dfs(visited)
        yield self

@dataclasses.dataclass
class CompileUnitCosts:
    compile_cost_us: dict[str, float]
    recompile_cost_us: dict[str, float]

def include_graph_from_include_tracker(dir: pathlib.Path, head_node_name: str, input_str: str) -> IncludeInfo:
    entries: dict[str, IncludeInfo] = dict()

    for line in input_str.strip().splitlines():
        if not line.startswith("# "):
            continue

        filename, includer_filename = line[2:].split(" | ")
        filename = str(pathlib.Path(filename).absolute())
        includer_filename = str(pathlib.Path(includer_filename).absolute())

        entry = entries.setdefault(filename, IncludeInfo(includees=[], filename=filename))
        includer_entry = entries.setdefault(includer_filename, IncludeInfo(includees=[], filename=includer_filename))

        assert(entry != includer_entry)

        if all(i != entry for i in includer_entry.includees):
            # Might be included multiple times
            includer_entry.includees.append(entry)

    return entries[str(dir / head_node_name)]

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
        cmd = cmd[:o_idx] + ["-Wno-everything"] + cmd[o_idx + 1:]

        tracker_path = pathlib.Path("cmake-build-debug/include-tracker").absolute()

        # TODO Check=false currently needed :/
        result = subprocess.run([tracker_path, *[f"--extra-arg={arg}" for arg in cmd[1:-1]], target_file], cwd=directory, check=False, capture_output=True, text=True)
        head_include_info = include_graph_from_include_tracker(directory, compile_unit_path, result.stdout)

        trace_json_path = tmp_dir / "trace.json"
        subprocess.run(cmd + ["-o", tmp_dir / "out.o", "-ftime-trace=" + str(trace_json_path)], cwd=directory, check=True, capture_output=True)
        self_times = self_times_from_time_trace_file(trace_json_path, dir=directory)

    deep_includes: dict[str, dict[str, int]] = dict()
    self_sizes: dict[str, int] = dict()

    directory_str = str(directory)

    def is_internal(path: str):
        # Ignore generated code, it likely won't change often.
        return path.startswith(directory_str) and not ("thirdparty" in path) and not (".gen." in path)

    # Visit deep nodes first, so that the deep includees are available when the parent is evaluating.
    for include_info in head_include_info.visit_dfs(set()):
        if is_internal(include_info.filename):
            self_sizes[include_info.filename] = (directory / include_info.filename).stat().st_size

        deep_includes_this = deep_includes[include_info.filename] = dict()
        for includee in include_info.includees:
            deep_includes_this[includee.filename] = 1

        for includee in include_info.includees:
            for grandchild_includee, grandchild_include_count in deep_includes[includee.filename].items():
                deep_includes_this[grandchild_includee] = grandchild_include_count + deep_includes_this.get(grandchild_includee, 0)

    # print(json.dumps(deep_includes[head_include_info.filename], indent=2))

    total_include_counts: dict[str, int] = deep_includes[head_include_info.filename]
    # Not included by anything explicitly, just add it so it's iterated.
    total_include_counts[head_include_info.filename] = 1

    deep_times: dict[str, int] = dict()
    deep_sizes: dict[str, int] = dict()

    # Attribute as much of the includee to us as how many includes are caused by us.
    for filename, include_count in total_include_counts.items():
        # We weigh our own cost to ourselves as 1.
        deep_time = self_times.get(filename, 0)
        deep_file_size = self_sizes.get(filename, 0)
        this_is_internal = is_internal(filename)

        for includee, includee_count in deep_includes[filename].items():
            weight = include_count * includee_count / total_include_counts[includee]
            assert(1 >= weight > 0)

            deep_time += self_times.get(includee, 0) * weight
            if this_is_internal and is_internal(includee):
                deep_file_size += self_sizes.get(includee, 0) * weight

        deep_times[filename] = deep_time
        if this_is_internal:
            deep_sizes[filename] = deep_file_size

    # end - start would be a better estimate, but this is good enough
    compile_unit_total_compile_time: float = sum(self_times.values())

    assert(all(t >= 0 for t in self_times.values()))
    assert(all(t >= 0 for t in deep_times.values()))

    # Should be true, but a lot of precision is lost during computation, so eh.
    # assert(abs(compile_unit_total_compile_time - deep_times[head_include_info.filename]) < 10000)

    if is_internal(head_include_info.filename):
        total_file_size: int = deep_sizes[head_include_info.filename]
        deep_recompile_times: dict[str, float] = {
            filename: deep_sizes[filename] / total_file_size * compile_unit_total_compile_time for filename, value in deep_sizes.items()
        }
         # + 1 to account for floating point inaccuracies
        assert(v <= compile_unit_total_compile_time + 1 for v in deep_recompile_times.values())
        assert(abs(deep_recompile_times[head_include_info.filename] - compile_unit_total_compile_time) < 1)
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

    # Test one to ensure that it runs properly.
    # evaluate_compile_entry(0, compile_commands[600])
    # exit(0)

    if limit >= 0:
        compile_commands = compile_commands[:limit]

    total_compile_costs_us: dict[str, float] = {}
    total_recompile_costs_us: dict[str, float] = {}

    failure_count = 0

    executor = concurrent.futures.ProcessPoolExecutor(multiprocessing.cpu_count())
    futures = [executor.submit(evaluate_compile_entry, *item) for item in enumerate(compile_commands)]
    completed, not_completed = concurrent.futures.wait(futures)

    print(f"Fetching results...")

    for future in completed:
        try:
            compile_unit_costs: CompileUnitCosts = future.result()
            for filename, cost in compile_unit_costs.compile_cost_us.items():
                total_compile_costs_us.setdefault(filename, 0)
                total_compile_costs_us[filename] += cost
            for filename, cost in compile_unit_costs.recompile_cost_us.items():
                total_recompile_costs_us.setdefault(filename, 0)
                total_recompile_costs_us[filename] += cost
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
