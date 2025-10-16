import pathlib
import subprocess
from builtins import FileNotFoundError
from collections import defaultdict
import datetime
import argparse

arg_parser = argparse.ArgumentParser(
    description='What the program does',
    epilog='Text at the bottom of help')

arg_parser.add_argument('repo_path')
arg_parser.add_argument('--months', required=False, default=6, type=float)

def main():
    args = arg_parser.parse_args()
    repo_path = args.repo_path

    # Calculate the date 6 months ago
    six_months_ago = (datetime.datetime.now() - datetime.timedelta(days=args.months * 30)).strftime("%Y-%m-%d")

    # Run git log to get all commit hashes from the last 6 months
    commit_hashes = subprocess.check_output(
        ["git", "log", f"--since={six_months_ago}", "--pretty=format:%H"],
        text=True, cwd=repo_path
    ).splitlines()

    # Run git log to capture renames in last 6 months
    renames_output = subprocess.run(
        ["git", "--no-pager", "log", "--name-status", "--diff-filter=R", f"--since={commit_hashes[-1]}"],
        capture_output=True,
        text=True,
        cwd=repo_path
    )
    rename_map: dict[str, str] = {}
    for line in renames_output.stdout.splitlines():
        parts = line.split()
        if parts and parts[0].startswith("R") and len(parts) == 3:
            _, old_name, new_name = parts
            rename_map[old_name] = rename_map.get(new_name, new_name)

    changes_by_file = defaultdict(int)

    for commit in commit_hashes:
        # Get the list of files changed in each commit
        changed_files = subprocess.check_output(
            ["git", "diff-tree", "--name-only", "--no-commit-id", commit, "-r"],
            text=True, cwd=repo_path
        ).splitlines()

        # Count each file
        for file in changed_files:
            if file.strip():  # skip empty lines
                changes_by_file[rename_map.get(file, file)] += 1

    out_str = "filename;changes;size\n"

    for subpath, changes in changes_by_file.items():
        try:
            file_size = (pathlib.Path(repo_path) / subpath).stat().st_size
        except FileNotFoundError:
            continue  # TODO

        out_str += f"\"{subpath}\";{changes};{file_size}\n"

    pathlib.Path("churn.csv").write_text(out_str)

if __name__ == "__main__":
    main()
