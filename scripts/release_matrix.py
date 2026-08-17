"""Run the six local uv-managed Python/dependency compatibility cells."""

from __future__ import annotations

import argparse
import subprocess
import tempfile
from pathlib import Path

PYTHONS = ("3.11", "3.12", "3.13")
LOWEST = (
    "fastmcp==3.1.0",
    "ipython==8.25.0",
    "pydantic==2.11.7",
    "pytest==8.0.0",
)


def run(command: list[str], root: Path) -> None:
    subprocess.run(command, cwd=root, check=True)


def run_cell(root: Path, python: str, dependency_set: str) -> None:
    command = [
        "uv",
        "run",
        "--isolated",
        "--no-project",
        "--python",
        python,
        "--with-editable",
        str(root),
    ]
    if dependency_set == "lowest":
        for requirement in LOWEST:
            command.extend(("--with", requirement))
    else:
        with tempfile.TemporaryDirectory(prefix="ipython-mcp-lock-") as directory:
            requirements = Path(directory) / "locked.txt"
            run(
                [
                    "uv",
                    "export",
                    "--quiet",
                    "--locked",
                    "--no-dev",
                    "--no-emit-project",
                    "--format",
                    "requirements-txt",
                    "--output-file",
                    str(requirements),
                ],
                root,
            )
            command.extend(("--with-requirements", str(requirements), "--with", "pytest>=8,<9"))
            command.extend(("pytest", "-q"))
            run(command, root)
            return
    command.extend(("pytest", "-q"))
    run(command, root)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", choices=PYTHONS)
    parser.add_argument("--set", dest="dependency_set", choices=("lowest", "locked"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    pythons = (args.python,) if args.python else PYTHONS
    sets = (args.dependency_set,) if args.dependency_set else ("lowest", "locked")
    for python in pythons:
        for dependency_set in sets:
            print(f"matrix python={python} dependencies={dependency_set}", flush=True)
            run_cell(root, python, dependency_set)


if __name__ == "__main__":
    main()
