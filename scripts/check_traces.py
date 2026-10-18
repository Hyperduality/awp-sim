"""Validate JSON Lines wire traces with the pinned spec's own checker (spec/scripts/validate.mjs).

The spec's checker validates every trace under examples/v0.1/traces, so this copies the spec to a
temporary directory, replaces its traces with the given files, and runs it there.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "spec"


def ensure_node_modules(npm: str) -> None:
    if not (SPEC / "node_modules").is_dir():
        subprocess.run([npm, "ci", "--silent"], cwd=SPEC, check=True)


def link_dir(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if sys.platform != "win32":
            raise
        # Symlinks need a privilege on Windows; a junction does not.
        import _winapi

        _winapi.CreateJunction(str(target), str(link))


def unlink_dir(link: Path) -> None:
    """Remove the link itself, so cleaning up the temporary copy never reaches its target."""
    try:
        link.unlink()
    except (IsADirectoryError, PermissionError):
        os.rmdir(link)


def check(paths: list[Path]) -> int:
    node, npm = shutil.which("node"), shutil.which("npm")
    if node is None or npm is None:
        print("node and npm are required to run the spec's trace checker", file=sys.stderr)
        return 2
    ensure_node_modules(npm)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "spec"
        shutil.copytree(SPEC, work, ignore=shutil.ignore_patterns(".git", "node_modules"))
        modules = work / "node_modules"
        link_dir(modules, SPEC / "node_modules")
        try:
            traces = work / "examples" / "v0.1" / "traces"
            shutil.rmtree(traces)
            traces.mkdir()
            for p in paths:
                shutil.copy(p, traces / p.name)
            proc = subprocess.run(
                [node, "scripts/validate.mjs"], cwd=work, capture_output=True, text=True
            )
        finally:
            unlink_dir(modules)
    lines = (proc.stdout + proc.stderr).splitlines()
    for line in lines:
        if line.startswith(("FAIL", "all checks")) or "trace " in line:
            print(line.replace("examples/v0.1/traces/", ""))
    return proc.returncode


def main() -> int:
    paths = [Path(p) for p in sys.argv[1:]]
    if not paths:
        print("usage: check_traces.py TRACE.jsonl...", file=sys.stderr)
        return 2
    return check(paths)


if __name__ == "__main__":
    sys.exit(main())
