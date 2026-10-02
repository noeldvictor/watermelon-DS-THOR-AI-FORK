"""Runs one of the repo's Python tools for the studio, so a job can be stopped cleanly.

    python runner.py <script.py> [args...]

The studio starts every Python job through this wrapper in its own process group. Cancel sends
CTRL_BREAK to that group; the handler below turns it into KeyboardInterrupt, so the tool's own
`finally` blocks still run (frame_compare restores the device settings it changed and closes the
emulator). Only if the tool doesn't stop within the grace period is the process tree killed.
"""
from __future__ import annotations

import os
import runpy
import signal
import sys


def _interrupt(signum, frame):  # noqa: ARG001 - signal handler signature
    raise KeyboardInterrupt


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: runner.py <script.py> [args...]", file=sys.stderr)
        return 2
    script = os.path.abspath(sys.argv[1])
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _interrupt)
    signal.signal(signal.SIGINT, _interrupt)
    sys.argv = [script] + sys.argv[2:]
    # the tool sees its own folder first on sys.path, as when it's run directly
    sys.path[0] = os.path.dirname(script)
    try:
        runpy.run_path(script, run_name="__main__")
    except KeyboardInterrupt:
        print("stopped (cancelled from the studio)", flush=True)
        return 130
    except SystemExit as e:
        if e.code is None or isinstance(e.code, int):
            return e.code or 0
        print(e.code, file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
