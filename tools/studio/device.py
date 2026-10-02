"""The AYN Thor over adb: status, screenshots of both displays, and the app's debug commands.

Everything goes through one `runner` callable so tests can swap adb out. The Thor is shared with
other sessions, so commands that change something on it call `guard()` first: they only run
when nothing but our app or the home screen is in front, unless the user confirmed.

The debug commands are broadcasts to the app's debug receiver (debug builds only), the same ones
tools/thor_mcp uses: `am broadcast -f 32 -p <pkg> -a <pkg>.<ACTION>`, answered in result data.
"""
from __future__ import annotations

import io
import json
import re
import shlex
import subprocess
import threading
import time
import urllib.parse
from typing import Any, Callable

from config import Settings

Runner = Callable[[list[str], float], tuple[int, bytes, bytes]]

STATE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")
ROM_LIST_ACTIVITY = "me.magnum.melonds.ui.romlist.RomListActivity"


def default_runner(cmd: list[str], timeout: float) -> tuple[int, bytes, bytes]:
    flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    r = subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=flags)
    return r.returncode, r.stdout, r.stderr


class DeviceError(RuntimeError):
    """Something on the device side failed; the message is meant for the user."""


class DeviceInputError(ValueError):
    """The user asked for something invalid (a bad state name); nothing was sent to the device."""


class NeedsConfirm(RuntimeError):
    """Another app is in front on the Thor: the user has to confirm before we touch it."""

    def __init__(self, foreground: dict[str, Any]):
        super().__init__(f"{foreground.get('label', 'Another app')} is in front on the Thor. Someone else "
                         "may be using it (it's shared). Run the command anyway?")
        self.foreground = foreground


class Device:
    def __init__(self, settings: Settings, runner: Runner = default_runner):
        self.settings = settings
        self.runner = runner
        self._status_lock = threading.Lock()
        self._status_cache: tuple[float, dict[str, Any]] | None = None
        self._shot_locks = {"top": threading.Lock(), "bottom": threading.Lock()}
        self._shots: dict[str, tuple[float, bytes, str]] = {}

    # ------------------------------------------------------------------ adb plumbing

    @property
    def package(self) -> str:
        return self.settings.get("package")

    def _run(self, cmd: list[str], timeout: float) -> tuple[int, bytes, bytes]:
        try:
            return self.runner(cmd, timeout)
        except FileNotFoundError:
            raise DeviceError("adb was not found. Install Android platform-tools and make sure adb is on PATH.") from None
        except subprocess.TimeoutExpired:
            raise DeviceError(f"adb did not answer within {timeout:.0f} s. Is the Thor awake and connected?") from None

    def devices(self) -> list[dict[str, str]]:
        code, out, err = self._run(["adb", "devices", "-l"], 15)
        if code != 0:
            raise DeviceError(f"adb devices failed: {err.decode(errors='replace').strip()}")
        rows = []
        for line in out.decode(errors="replace").splitlines()[1:]:
            parts = line.split()
            if len(parts) < 2:
                continue
            info = {"serial": parts[0], "state": parts[1], "model": ""}
            for p in parts[2:]:
                if p.startswith("model:"):
                    info["model"] = p[6:].replace("_", " ")
            rows.append(info)
        return rows

    def serial(self, devices: list[dict[str, str]] | None = None) -> str:
        """The configured serial, or (when it's blank) the attached AYN Thor."""
        configured = self.settings.get("serial") or ""
        if configured:
            return configured
        devices = devices if devices is not None else self.devices()
        ready = [d for d in devices if d["state"] == "device"]
        thor = [d["serial"] for d in ready if "Thor" in d["model"]]
        if thor:
            return thor[0]
        if len(ready) == 1:
            return ready[0]["serial"]
        raise DeviceError("No AYN Thor is connected. Plug it in (USB debugging on) or set its serial in Settings.")

    def adb(self, *args: str, timeout: float = 30, binary: bool = False, check: bool = True) -> Any:
        cmd = ["adb", "-s", self.serial(), *args]
        code, out, err = self._run(cmd, timeout)
        if check and code != 0:
            msg = err.decode(errors="replace").strip() or out.decode(errors="replace").strip()
            if "not found" in msg and "device" in msg:
                raise DeviceError(f"The Thor ({self.serial()}) isn't connected: {msg}")
            raise DeviceError(f"adb {' '.join(args[:2])} failed: {msg}")
        return out if binary else out.decode(errors="replace")

    def shell(self, command: str, **kw: Any) -> str:
        return self.adb("shell", command, **kw)

    def broadcast(self, action: str, timeout: float = 60, **extras: Any) -> str:
        """Send a debug command to the app and return its result data (raises on failure)."""
        pkg = self.package
        parts = ["am", "broadcast", "-f", "32", "-p", pkg, "-a", f"{pkg}.{action}"]
        for key, value in extras.items():
            if value is None:
                continue
            if isinstance(value, bool):
                parts += ["--ez", key, "true" if value else "false"]
            elif isinstance(value, int):
                parts += ["--ei", key, str(value)]
            else:
                parts += ["--es", key, str(value)]
        out = self.shell(" ".join(shlex.quote(p) for p in parts), timeout=timeout)
        m = re.search(r'result=(-?\d+)(?:, data="(.*)")?\s*$', out.strip(), re.S)
        if not m:
            raise DeviceError(f"The app did not answer {action}. Is a debug build of Watermelon Thor installed? ({out.strip()[:200]})")
        code, data = int(m.group(1)), m.group(2) or ""
        if code != 1:
            raise DeviceError(f"{action} failed on the device: {data or out.strip()}")
        return data

    # ------------------------------------------------------------------ state

    def classify(self, activity: str) -> dict[str, Any]:
        pkg = activity.split("/", 1)[0] if activity else ""
        markers = [m.lower() for m in self.settings.get("idle_markers") or []]
        if not activity:
            kind, label = "unknown", "Nothing reported"
        elif pkg == self.package:
            if "EmulatorActivity" in activity:
                kind, label = "emulator", "Watermelon Thor: a game is running"
            else:
                kind, label = "app", "Watermelon Thor: " + _short_activity(activity)
        elif any(m in activity.lower() for m in markers):
            kind, label = "launcher", "Home screen (idle)"
        else:
            kind, label = "other", f"Another app: {pkg}"
        return {"activity": activity, "package": pkg, "kind": kind, "label": label}

    def foreground(self) -> dict[str, Any]:
        """What's resumed on each display; `kind` of the whole is the most 'busy' one."""
        out = self.shell("dumpsys activity activities | grep -E 'ResumedActivity'", timeout=20, check=False)
        return self._foreground_from(out)

    def _foreground_from(self, text: str) -> dict[str, Any]:
        tops = re.findall(r"topResumedActivity=ActivityRecord\{\S+ u\d+ ([^\s}]+)", text)
        focused = re.findall(r"\bResumedActivity: ActivityRecord\{\S+ u\d+ ([^\s}]+)", text)
        seen: list[str] = []
        for a in focused + tops:
            if a not in seen:
                seen.append(a)
        items = [self.classify(a) for a in seen]
        if not items:
            info = self.classify("")
        else:
            rank = {"other": 3, "emulator": 2, "app": 1, "launcher": 0, "unknown": 0}
            info = dict(max(items, key=lambda i: rank[i["kind"]]))
        info["displays"] = items
        return info

    def status(self, max_age: float = 1.5) -> dict[str, Any]:
        """Connection, foreground, FPS. Cached briefly so several tabs don't multiply adb calls."""
        with self._status_lock:
            if self._status_cache and time.time() - self._status_cache[0] < max_age:
                return self._status_cache[1]
            info = self._status()
            self._status_cache = (time.time(), info)
            return info

    def _status(self) -> dict[str, Any]:
        info: dict[str, Any] = {"adb": True, "connected": False, "serial": self.settings.get("serial") or "",
                                "model": "", "devices": [], "foreground": None, "app_running": False,
                                "fps": None, "awake": None, "texture_packs": None, "message": ""}
        try:
            devices = self.devices()
        except DeviceError as e:
            info["adb"] = "adb was not found" not in str(e)
            info["message"] = str(e)
            return info
        info["devices"] = devices
        try:
            serial = self.serial(devices)
        except DeviceError as e:
            info["message"] = str(e)
            return info
        info["serial"] = serial
        row = next((d for d in devices if d["serial"] == serial), None)
        if row is None or row["state"] != "device":
            thor = next((d for d in devices if "Thor" in d["model"] and d["state"] == "device"), None)
            if row is not None:
                info["message"] = f"The device {serial} is '{row['state']}'. Unlock it and accept the USB debugging prompt."
            elif thor:
                info["message"] = f"The Thor is attached as {thor['serial']}, not {serial}."
                info["suggested_serial"] = thor["serial"]
            else:
                info["message"] = f"The Thor ({serial}) isn't connected. Plug it in or check wireless debugging."
            return info
        info["connected"] = True
        info["model"] = row["model"]
        pkg = self.package
        # one adb round trip for everything the panel polls
        out = self.shell("dumpsys activity activities | grep -E 'ResumedActivity'; echo '@@'; "
                         f"pidof {shlex.quote(pkg)}; echo '@@'; dumpsys power | grep -m1 mWakefulness=",
                         timeout=20, check=False)
        parts = out.split("@@")
        info["foreground"] = self._foreground_from(parts[0])
        info["app_running"] = len(parts) > 1 and bool(parts[1].strip())
        if len(parts) > 2:
            m = re.search(r"mWakefulness=(\w+)", parts[2])
            info["awake"] = (m.group(1) == "Awake") if m else None
        if info["app_running"]:
            # only while the app runs: a broadcast to a stopped app would start its process
            try:
                info["fps"] = json.loads(self.broadcast("GET_FPS", timeout=10)).get("fps")
            except (DeviceError, ValueError):
                info["fps"] = None
            try:
                prefs = json.loads(self.broadcast("GET_PREFERENCES", timeout=10, filter="enable_texture_packs"))
                info["texture_packs"] = bool(prefs.get("enable_texture_packs", True))
            except (DeviceError, ValueError):
                pass
        return info

    def guard(self, confirm: bool) -> dict[str, Any]:
        """Raise NeedsConfirm if another app is in front (unless confirmed). Returns the foreground."""
        fg = self.foreground()
        if fg["kind"] == "other" and not confirm:
            raise NeedsConfirm(fg)
        return fg

    def invalidate(self) -> None:
        with self._status_lock:
            self._status_cache = None

    # ------------------------------------------------------------------ screens

    def screenshot(self, which: str, max_width: int = 720, max_age: float = 0.8) -> tuple[bytes, str]:
        """One display as JPEG (PNG when Pillow is missing). `which` is 'top' or 'bottom'."""
        if which not in self._shot_locks:
            raise DeviceInputError("display must be top or bottom")
        with self._shot_locks[which]:
            cached = self._shots.get(which)
            if cached and time.time() - cached[0] < max_age:
                return cached[1], cached[2]
            extra = [] if which == "top" else ["-d", self.settings.get("bottom_display")]
            png = self.adb("exec-out", "screencap", "-p", *extra, binary=True, timeout=20)
            if not png.startswith(b"\x89PNG"):
                raise DeviceError(f"The {which} screen could not be captured. Is the Thor awake?")
            data, mime = _shrink(png, max_width)
            self._shots[which] = (time.time(), data, mime)
            return data, mime

    # ------------------------------------------------------------------ commands

    def list_roms(self, query: str = "") -> list[dict[str, str]]:
        data = self.broadcast("LIST_ROMS", timeout=60, query=query or None)
        try:
            return json.loads(data)
        except ValueError:
            raise DeviceError(f"LIST_ROMS answered something unexpected: {data[:200]}") from None

    def rom_uri(self, file_name: str) -> str:
        return self.settings.get("rom_tree_uri") + urllib.parse.quote(file_name)

    def launch(self, uri: str) -> dict[str, Any]:
        pkg = self.package
        if self.foreground()["package"] != pkg:
            # Android won't let the app open the emulator from the background; with its ROM
            # list in front the same command works
            self.shell(f"am start -n {shlex.quote(pkg + '/' + ROM_LIST_ACTIVITY)}", timeout=20)
            time.sleep(2.5)
        reply = self.broadcast("LAUNCH_ROM", timeout=180, rom_uri=uri, wait_rom_ready=True)
        self.invalidate()
        return {"launched": uri, "reply": reply, "foreground": self.foreground()}

    def state_path(self, name: str) -> str:
        name = name.strip()
        if name.endswith(".ml"):
            name = name[:-3]
        if not STATE_NAME.match(name):
            raise DeviceInputError("Use a state name of letters, digits, '-', '_' or '.', up to 64 characters.")
        return f"/data/user/0/{self.package}/files/{name}.ml"

    def save_state(self, name: str) -> dict[str, Any]:
        path = self.state_path(name)
        reply = self.broadcast("SAVE_STATE", timeout=60, path=path)
        return {"saved": path, "reply": reply}

    def load_state(self, name: str) -> dict[str, Any]:
        path = self.state_path(name)
        reply = self.broadcast("LOAD_STATE", timeout=60, path=path)
        return {"loaded": path, "reply": reply}

    def list_states(self) -> list[str]:
        out = self.adb("shell", f"run-as {shlex.quote(self.package)} ls files", timeout=20, check=False)
        return sorted(line.strip() for line in out.splitlines() if line.strip().endswith(".ml"))

    def preferences(self, filter_text: str = "") -> dict[str, Any]:
        data = self.broadcast("GET_PREFERENCES", timeout=30, filter=filter_text or None)
        try:
            return json.loads(data)
        except ValueError:
            raise DeviceError(f"GET_PREFERENCES answered something unexpected: {data[:200]}") from None

    def set_preference(self, key: str, value: str, kind: str | None = None) -> dict[str, Any]:
        data = self.broadcast("SET_PREFERENCE", timeout=30, key=key, value=value, type=kind)
        try:
            return json.loads(data)
        except ValueError:
            return {"reply": data}

    def set_texture_packs(self, on: bool) -> dict[str, Any]:
        result = self.set_preference("enable_texture_packs", "true" if on else "false", "boolean")
        self.invalidate()
        return result

    def close_emulator(self) -> dict[str, Any]:
        self.shell(f"am force-stop {shlex.quote(self.package)}", timeout=20)
        self.invalidate()
        return {"stopped": self.package}


def _short_activity(activity: str) -> str:
    name = activity.rsplit(".", 1)[-1].rsplit("/", 1)[-1]
    if "RomList" in name:
        return "ROM list"
    return re.sub(r"Activity$", "", name) or name


def _shrink(png: bytes, max_width: int) -> tuple[bytes, str]:
    try:
        from PIL import Image
    except ImportError:
        return png, "image/png"
    im = Image.open(io.BytesIO(png)).convert("RGB")
    if max_width and im.width > max_width:
        im = im.resize((max_width, max(1, round(im.height * max_width / im.width))), Image.BILINEAR)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=82)
    return buf.getvalue(), "image/jpeg"
