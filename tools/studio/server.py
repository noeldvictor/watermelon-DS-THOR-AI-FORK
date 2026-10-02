"""HTTP layer: JSON API, static files and Server-Sent Events for job output.

Binds to 127.0.0.1 only. Because the API starts processes and drives the device, requests must
carry a local Host header, and POSTs must be JSON from our own origin: a web page elsewhere can't
make the browser call it (no CORS is ever granted, and the Origin and Sec-Fetch-Site checks refuse
cross-site posts).
"""
from __future__ import annotations

import json
import re
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from app import NotFound, StudioApp
from checks import ChecksError
from config import STATIC_DIR, SettingsError
from device import DeviceError, DeviceInputError, NeedsConfirm
from pipeline import PipelineError

MAX_BODY = 1 << 20
SSE_BATCH = 400
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8", ".txt": "text/plain; charset=utf-8",
}

Route = tuple[str, "re.Pattern[str]", Callable[..., None]]
ROUTES: list[Route] = []


def route(method: str, pattern: str) -> Callable[[Callable[..., None]], Callable[..., None]]:
    def deco(fn: Callable[..., None]) -> Callable[..., None]:
        ROUTES.append((method, re.compile(pattern), fn))
        return fn
    return deco


class StudioServer(ThreadingHTTPServer):
    daemon_threads = True
    # on Windows SO_REUSEADDR lets a second server bind the same port; refuse instead
    allow_reuse_address = False

    def __init__(self, app: StudioApp, address: tuple[str, int]):
        self.app = app
        super().__init__(address, Handler)

    @property
    def port(self) -> int:
        return self.server_address[1]


class Handler(BaseHTTPRequestHandler):
    server: StudioServer
    server_version = "RemasterStudio/1"

    # ------------------------------------------------------------------ plumbing

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        pass  # the browser polls; per-request logs would drown the console

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def _allowed_hosts(self) -> set[str]:
        port = self.server.port
        return {f"127.0.0.1:{port}", f"localhost:{port}", "127.0.0.1", "localhost"}

    def _dispatch(self, method: str) -> None:
        host = (self.headers.get("Host") or "").lower()
        if host not in self._allowed_hosts():
            self._json(403, {"error": "The studio only answers on 127.0.0.1."})
            return
        if method == "POST":
            origin = self.headers.get("Origin")
            port = self.server.port
            if origin and origin not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
                self._json(403, {"error": "Requests from other web pages are refused."})
                return
            if (self.headers.get("Sec-Fetch-Site") or "same-origin") not in ("same-origin", "none"):
                self._json(403, {"error": "Requests from other web pages are refused."})
                return
            if "application/json" not in (self.headers.get("Content-Type") or ""):
                self._json(415, {"error": "Send JSON (Content-Type: application/json)."})
                return
        url = urlsplit(self.path)
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        for m, pattern, fn in ROUTES:
            if m != method:
                continue
            match = pattern.fullmatch(url.path)
            if match:
                try:
                    fn(self, *match.groups(), query=query)
                except NeedsConfirm as e:
                    self._json(409, {"error": str(e), "needs_confirm": True, "foreground": e.foreground})
                except (PipelineError, ChecksError, SettingsError, DeviceInputError) as e:
                    self._json(400, {"error": str(e)})
                except DeviceError as e:
                    self._json(503, {"error": str(e)})
                except NotFound as e:
                    self._json(404, {"error": str(e)})
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
                except Exception as e:  # report, don't crash the request thread silently
                    traceback.print_exc(file=sys.stderr)
                    try:
                        self._json(500, {"error": f"Something went wrong in the studio: {type(e).__name__}: {e}"})
                    except OSError:
                        pass
                return
        self._json(404, {"error": f"Nothing at {url.path}"})

    def body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise PipelineError("Request too large.")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, ValueError):
            raise PipelineError("The request body isn't valid JSON.") from None
        if not isinstance(data, dict):
            raise PipelineError("The request body must be a JSON object.")
        return data

    def _send(self, status: int, data: bytes, content_type: str, cache: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _json(self, status: int, obj: Any) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def ok(self, obj: Any) -> None:
        self._json(200, obj)

    def send_file(self, path: Path, cache: str = "no-cache") -> None:
        ctype = CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
        self._send(200, path.read_bytes(), ctype, cache)

    @property
    def app(self) -> StudioApp:
        return self.server.app


# ---------------------------------------------------------------------------- static

@route("GET", r"/")
def index(h: Handler, query: dict[str, str]) -> None:
    h.send_file(STATIC_DIR / "index.html")


@route("GET", r"/static/(.+)")
def static(h: Handler, rel: str, query: dict[str, str]) -> None:
    root = STATIC_DIR.resolve()
    p = (root / rel).resolve()
    try:
        p.relative_to(root)
    except ValueError:
        raise NotFound("no such file") from None
    if not p.is_file():
        raise NotFound("no such file")
    h.send_file(p)


# ---------------------------------------------------------------------------- settings, setup

@route("GET", r"/api/health")
def health(h: Handler, query: dict[str, str]) -> None:
    h.ok({"ok": True, "name": "Watermelon Remaster Studio"})


@route("GET", r"/api/settings")
def settings_get(h: Handler, query: dict[str, str]) -> None:
    s = h.app.settings
    h.ok({"settings": s.snapshot(), "file": str(s.path), "hd_remaster_dir": str(s.hd_dir()),
          "checks_out_dir": str(s.checks_out_dir()), "python": s.python()})


@route("POST", r"/api/settings")
def settings_post(h: Handler, query: dict[str, str]) -> None:
    body = h.body()
    snapshot = h.app.settings.update(body.get("settings") or {})
    h.app.device.invalidate()
    h.ok({"settings": snapshot, "file": str(h.app.settings.path)})


@route("GET", r"/api/setup")
def setup_get(h: Handler, query: dict[str, str]) -> None:
    h.ok(h.app.setup_status())


@route("POST", r"/api/setup/run")
def setup_run(h: Handler, query: dict[str, str]) -> None:
    h.body()
    h.ok({"job": h.app.run_setup()})


# ---------------------------------------------------------------------------- games

@route("GET", r"/api/games")
def games(h: Handler, query: dict[str, str]) -> None:
    h.ok(h.app.games())


@route("GET", r"/api/roms")
def roms(h: Handler, query: dict[str, str]) -> None:
    h.ok(h.app.roms_found())


@route("POST", r"/api/games/new")
def games_new(h: Handler, query: dict[str, str]) -> None:
    body = h.body()
    h.ok(h.app.new_game(str(body.get("rom_path") or ""), start_extract=body.get("extract", True) is not False))


@route("GET", r"/api/games/([A-Za-z0-9]{4})")
def game(h: Handler, code: str, query: dict[str, str]) -> None:
    h.ok(h.app.game(code))


@route("POST", r"/api/games/([A-Za-z0-9]{4})/rom")
def game_rom(h: Handler, code: str, query: dict[str, str]) -> None:
    h.ok(h.app.set_rom(code, str(h.body().get("rom_path") or "")))


@route("POST", r"/api/games/([A-Za-z0-9]{4})/run")
def game_run(h: Handler, code: str, query: dict[str, str]) -> None:
    body = h.body()
    options = body.get("options") or {}
    if not isinstance(options, dict):
        raise PipelineError("options must be an object")
    h.ok({"job": h.app.run_step(code, str(body.get("step") or ""), options, bool(body.get("confirm")))})


@route("GET", r"/api/games/([A-Za-z0-9]{4})/media/([^/]+)")
def game_media(h: Handler, code: str, name: str, query: dict[str, str]) -> None:
    p = h.app.library.media_path(code, name)
    if p is None:
        raise NotFound("no such image")
    h.send_file(p, cache="max-age=3600")


# ---------------------------------------------------------------------------- 3D models

@route("GET", r"/api/games/([A-Za-z0-9]{4})/models3d")
def game_models3d(h: Handler, code: str, query: dict[str, str]) -> None:
    h.ok(h.app.models3d(code))


@route("GET", r"/api/games/([A-Za-z0-9]{4})/models3d/([A-Za-z0-9._-]+)")
def game_model3d(h: Handler, code: str, model_id: str, query: dict[str, str]) -> None:
    h.ok(h.app.model3d(code, model_id))


@route("GET", r"/api/games/([A-Za-z0-9]{4})/models3d/([A-Za-z0-9._-]+)/file/((?:ai/)?[A-Za-z0-9._-]+\.png)")
def game_model3d_file(h: Handler, code: str, model_id: str, rel: str, query: dict[str, str]) -> None:
    # only PNGs inside work/<CODE>/models/<id>/ and its ai/ folder (Library.model_file checks)
    p = h.app.library.model_file(code, model_id, rel)
    if p is None:
        raise NotFound("no such image")
    h.send_file(p)


# ---------------------------------------------------------------------------- jobs

@route("GET", r"/api/jobs")
def jobs(h: Handler, query: dict[str, str]) -> None:
    h.ok(h.app.job_list(game=query.get("game") or None, kind=query.get("kind") or None))


@route("GET", r"/api/jobs/(j\d+)")
def job(h: Handler, job_id: str, query: dict[str, str]) -> None:
    j = h.app.job(job_id)
    tail = max(0, min(int(query.get("tail") or 0), 5000)) if (query.get("tail") or "0").isdigit() else 0
    h.ok({"job": j.summary(), "tail": j.tail(tail)})


@route("POST", r"/api/jobs/(j\d+)/cancel")
def job_cancel(h: Handler, job_id: str, query: dict[str, str]) -> None:
    h.body()
    h.ok({"job": h.app.cancel_job(job_id)})


@route("GET", r"/api/jobs/(j\d+)/events")
def job_events(h: Handler, job_id: str, query: dict[str, str]) -> None:
    """Server-Sent Events: 'lines' batches (id = last line index), 'status' changes, then 'end'."""
    j = h.app.job(job_id)
    last = h.headers.get("Last-Event-ID") or ""
    start = int(last) + 1 if last.isdigit() else (int(query["from"]) if query.get("from", "").isdigit() else 0)
    h.send_response(200)
    h.send_header("Content-Type", "text/event-stream; charset=utf-8")
    h.send_header("Cache-Control", "no-store")
    h.send_header("X-Accel-Buffering", "no")
    h.end_headers()
    out = h.wfile

    def event(name: str, data: Any, event_id: int | None = None) -> None:
        head = f"id: {event_id}\n" if event_id is not None else ""
        out.write(f"{head}event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8"))

    try:
        out.write(b"retry: 2000\n\n")
        sent_status = None
        while True:
            first, lines, status = j.read(start, wait=15.0)
            for i in range(0, len(lines), SSE_BATCH):
                chunk = lines[i:i + SSE_BATCH]
                event("lines", {"from": first + i, "lines": chunk}, first + i + len(chunk) - 1)
            start = first + len(lines)
            if status != sent_status:
                event("status", j.summary())
                sent_status = status
            elif not lines:
                out.write(b": ping\n\n")
            if j.finished and start >= j.line_count:
                event("end", j.summary())
                out.flush()
                return
            out.flush()
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
        return


# ---------------------------------------------------------------------------- device

@route("GET", r"/api/device/status")
def device_status(h: Handler, query: dict[str, str]) -> None:
    h.ok(h.app.device.status())


@route("GET", r"/api/device/screen/(top|bottom)")
def device_screen(h: Handler, which: str, query: dict[str, str]) -> None:
    width = int(query["w"]) if query.get("w", "").isdigit() else 720
    data, mime = h.app.device.screenshot(which, max(160, min(width, 1920)))
    h._send(200, data, mime)


@route("GET", r"/api/device/roms")
def device_roms(h: Handler, query: dict[str, str]) -> None:
    h.ok({"roms": h.app.device.list_roms(query.get("query", ""))})


@route("GET", r"/api/device/states")
def device_states(h: Handler, query: dict[str, str]) -> None:
    h.ok({"states": h.app.device.list_states()})


@route("POST", r"/api/device/(launch|save_state|load_state|texture_packs|close)")
def device_action(h: Handler, action: str, query: dict[str, str]) -> None:
    h.ok(h.app.device_action(action, h.body()))


# ---------------------------------------------------------------------------- checks

@route("GET", r"/api/checks/cases")
def checks_cases(h: Handler, query: dict[str, str]) -> None:
    h.ok({"files": h.app.checks.case_files()})


@route("GET", r"/api/checks/runs")
def checks_runs(h: Handler, query: dict[str, str]) -> None:
    h.ok({"runs": h.app.checks.runs(), "out_dir": str(h.app.settings.checks_out_dir()),
          "roots": [str(r) for r in h.app.checks.roots()]})


@route("GET", r"/api/checks/run")
def checks_run_detail(h: Handler, query: dict[str, str]) -> None:
    h.ok(h.app.checks.run_detail(query.get("id", "")))


@route("GET", r"/api/checks/file")
def checks_file(h: Handler, query: dict[str, str]) -> None:
    p = h.app.checks.resolve(query.get("id", ""))
    if p is None or not p.is_file() or p.suffix.lower() not in (".png", ".json"):
        raise NotFound("no such file")
    h.send_file(p, cache="max-age=600")


@route("POST", r"/api/checks/run")
def checks_start(h: Handler, query: dict[str, str]) -> None:
    body = h.body()
    h.ok({"job": h.app.run_checks(body.get("options") or {}, bool(body.get("confirm")))})


def make_server(app: StudioApp, host: str = "127.0.0.1", port: int = 8765) -> StudioServer:
    if host not in ("127.0.0.1", "localhost"):
        raise ValueError("the studio binds to 127.0.0.1 only")
    return StudioServer(app, (host, port))
