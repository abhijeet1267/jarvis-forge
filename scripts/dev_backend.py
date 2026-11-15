#!/usr/bin/env python3
"""Restart the dev backend, but refuse while the test suite is using it.

Several test files read the live backend on :8765 (they pin its real wire shape).
Restarting it mid-run produced misleading failures -- files that pass in
isolation -- and that cost several full runs before it was recognised as a
process problem rather than a code problem.

This wrapper makes the mistake impossible instead of remembering not to make it:
it looks for a running pytest and refuses, with the reason. Pass --force only
when you genuinely mean to interrupt the suite.

Usage:
    python scripts/dev_backend.py restart
    python scripts/dev_backend.py status
"""

from __future__ import annotations

import subprocess
import sys


def _suite_running() -> bool:
    """Whether a real pytest process is alive.

    `pgrep -f` matches any command line containing the pattern -- including a
    monitor shell whose own text contains it, which made this report a suite
    forever after one monitor was left running. Only a process whose executable
    is actually python running pytest counts.
    """
    try:
        result = subprocess.run(["pgrep", "-fl", "pytest"],
                                capture_output=True, text=True)
    except FileNotFoundError:
        return False
    for line in result.stdout.splitlines():
        lowered = line.lower()
        if "python" in lowered and "pytest" in lowered and "pgrep" not in lowered:
            return True
    return False


def main(argv: list[str]) -> int:
    action = argv[1] if len(argv) > 1 else "status"
    force = "--force" in argv

    if action == "status":
        print("suite running" if _suite_running() else "suite idle")
        return 0

    if action != "restart":
        print(f"unknown action {action!r}; use status or restart", file=sys.stderr)
        return 2

    if _suite_running() and not force:
        print(
            "refusing to restart the backend: the test suite is running against it.\n"
            "Restarting now makes the live-backend tests fail for a reason that has\n"
            "nothing to do with the code. Wait for the suite, or pass --force if you\n"
            "really mean to interrupt it.",
            file=sys.stderr)
        return 1

    subprocess.run(["pkill", "-f", "run.py"], check=False)
    subprocess.run(
        "nohup .venv/bin/python run.py > /tmp/vantage_backend.log 2>&1 < /dev/null &",
        shell=True, check=False)
    print("backend restart requested")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))