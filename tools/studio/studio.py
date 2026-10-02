"""Watermelon Remaster Studio: a local web app for the HD remastering pipeline and the AYN Thor.

    python tools/studio/studio.py [--port 8765] [--no-browser] [--settings FILE]

Serves http://127.0.0.1:8765 (port from studio_settings.json unless --port is given; when another
program holds it, the next free port) and opens it in the browser. If the studio already runs
there, that one is opened instead. Ctrl+C stops the server and any job still running.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import urllib.request
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from app import StudioApp  # noqa: E402
from config import DEFAULT_SETTINGS_PATH  # noqa: E402
from server import make_server  # noqa: E402


def studio_running(host: str, port: int) -> bool:
    """Whether the program on host:port is this studio (then we open it instead of starting another)."""
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/health", timeout=2) as r:
            return json.loads(r.read().decode("utf-8")).get("name") == "Watermelon Remaster Studio"
    except (OSError, ValueError):
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, help="port (default: the settings file's, 8765)")
    ap.add_argument("--no-browser", action="store_true", help="don't open the browser")
    ap.add_argument("--settings", default=str(DEFAULT_SETTINGS_PATH), help="settings file")
    args = ap.parse_args()

    app = StudioApp(args.settings)
    host = app.settings.get("host") or "127.0.0.1"
    port = args.port or int(app.settings.get("port") or 8765)
    browser = not args.no_browser and app.settings.get("open_browser", True)
    server = None
    for candidate in range(port, port + 10):
        try:
            server = make_server(app, host, candidate)
            break
        except OSError as e:
            if studio_running(host, candidate):
                url = f"http://{host}:{candidate}/"
                print(f"The studio is already running on {url}; opening it.", flush=True)
                if browser:
                    webbrowser.open(url)
                return 0
            print(f"Port {candidate} is in use by another program ({e.strerror or e}); trying {candidate + 1}.",
                  file=sys.stderr, flush=True)
    if server is None:
        print(f"No free port between {port} and {port + 9}. Start with --port <another>.", file=sys.stderr)
        return 1
    url = f"http://{host}:{server.port}/"
    print(f"Watermelon Remaster Studio on {url}  (Ctrl+C to stop)", flush=True)
    print(f"settings: {app.settings.path}", flush=True)
    if browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("stopping", flush=True)
    finally:
        app.jobs.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
