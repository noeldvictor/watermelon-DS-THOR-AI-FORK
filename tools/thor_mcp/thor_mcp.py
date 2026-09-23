"""MCP server for driving Watermelon Thor on a device over adb.

Claude Code (or any MCP client) starts it over stdio; see .mcp.json at the repo root. Every tool
talks to the device through adb: settings, the ROM library, launching and FPS go through the
app's debug command receiver (debug builds, app/src/debug/.../DebugCommandReceiver.kt) and come
back as the broadcast's result data; screenshots of both displays, logs and pack folders go
through adb directly, since the app can't capture the second display itself.

The device is the attached AYN Thor unless THOR_SERIAL says otherwise. Standard library only;
Pillow is used to shrink screenshots when it's installed.
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import shlex
import subprocess
import sys
import time

PACKAGE = os.environ.get("THOR_PACKAGE", "app.watermelonthor.dev")
BOTTOM_DISPLAY = os.environ.get("THOR_BOTTOM_DISPLAY", "4630946482288158084")
PROTOCOL = "2025-06-18"

_serial: str | None = None


# ---------------------------------------------------------------------------- adb

def adb(*args: str, binary: bool = False, timeout: float = 60, check: bool = True):
    cmd = ["adb", "-s", device()] + list(args)
    r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)}: {r.stderr.decode(errors='replace').strip()}")
    return r.stdout if binary else r.stdout.decode(errors="replace")


def device() -> str:
    global _serial
    if _serial:
        return _serial
    if os.environ.get("THOR_SERIAL"):
        _serial = os.environ["THOR_SERIAL"]
        return _serial
    out = subprocess.run(["adb", "devices", "-l"], capture_output=True, text=True).stdout
    rows = [l for l in out.splitlines()[1:] if " device " in l]
    thor = [l.split()[0] for l in rows if "model:AYN_Thor" in l]
    if thor:
        _serial = thor[0]
    elif len(rows) == 1:
        _serial = rows[0].split()[0]
    else:
        raise RuntimeError("no AYN Thor attached (set THOR_SERIAL to pick a device)")
    return _serial


def shell(command: str, **kw) -> str:
    return adb("shell", command, **kw)


def broadcast(action: str, **extras) -> str:
    """Send a debug command to the app and return its result data."""
    # -f 32 is FLAG_INCLUDE_STOPPED_PACKAGES: after a force-stop Android skips broadcasts to
    # the app until it's started again, and these commands must work from any state
    parts = ["am", "broadcast", "-f", "32", "-a", f"{PACKAGE}.{action}", "-p", PACKAGE]
    for key, value in extras.items():
        if value is None:
            continue
        if isinstance(value, bool):
            parts += ["--ez", key, "true" if value else "false"]
        elif isinstance(value, int):
            parts += ["--ei", key, str(value)]
        else:
            parts += ["--es", key, str(value)]
    out = shell(" ".join(shlex.quote(p) for p in parts), timeout=120)
    m = re.search(r'result=(-?\d+)(?:, data="(.*)")?\s*$', out.strip(), re.S)
    if not m:
        raise RuntimeError(f"no reply from the app to {action}: {out.strip()}")
    code, data = int(m.group(1)), m.group(2) or ""
    if code != 1:
        raise RuntimeError(f"{action} failed: {data or out.strip()}")
    return data


def foreground() -> str:
    out = shell("dumpsys activity activities | grep -E ' ResumedActivity' | head -1")
    m = re.search(r"u0 ([^\s}]+)", out)
    return m.group(1) if m else out.strip()


def app_running() -> bool:
    return bool(shell(f"pidof {PACKAGE}", check=False).strip())


# ---------------------------------------------------------------------------- tools

def t_status(_):
    fg = foreground()
    info = {"device": device(), "foreground": fg, "app_running": app_running(),
            "emulator_in_front": fg.startswith(PACKAGE + "/") and "EmulatorActivity" in fg}
    pkg = shell(f"dumpsys package {PACKAGE} | grep -E 'versionName|lastUpdateTime' | head -2")
    info["installed"] = " ".join(pkg.split())
    if info["app_running"]:
        try:
            info.update(json.loads(broadcast("GET_FPS")))
        except Exception as e:  # an old build without GET_FPS
            info["fps_error"] = str(e)
        info["stats"] = latest_stats()
    if "launcher" in fg.lower():
        info["note"] = "the home screen is in front: the Thor is idle"
    elif not fg.startswith(PACKAGE + "/"):
        info["note"] = "another app is in front: the Thor may be in use by another session"
    return info


def latest_stats() -> dict:
    lines = shell("logcat -d -t 400 -s melonDS:W", timeout=30).splitlines()
    out = {}
    for tag in ("HDTexPack[Stats]", "VulkanOutput[Stats]", "HDTexPack: indexed"):
        hit = [l for l in lines if tag in l]
        if hit:
            out[tag] = hit[-1].split(tag, 1)[1].lstrip(": ").strip()
    return out


def t_settings_get(a):
    data = broadcast("GET_PREFERENCES", filter=a.get("filter"))
    return json.loads(data)


def t_settings_set(a):
    return json.loads(broadcast("SET_PREFERENCE", key=a["key"], value=str(a["value"]).lower()
                                if isinstance(a["value"], bool) else str(a["value"]), type=a.get("type")))


def t_list_roms(a):
    return json.loads(broadcast("LIST_ROMS", query=a.get("query")))


def t_launch(a):
    uri = a.get("uri")
    if not uri:
        roms = json.loads(broadcast("LIST_ROMS", query=a.get("query", "")))
        if not roms:
            raise RuntimeError(f"no ROM matches {a.get('query')!r}")
        if len(roms) > 1 and not a.get("first"):
            return {"error": "several ROMs match; pass a narrower query, first=true, or a uri",
                    "matches": [r["name"] for r in roms[:20]]}
        uri = roms[0]["uri"]
    if not foreground().startswith(PACKAGE + "/"):
        # Android won't let the app open the emulator from the background; with its ROM list in
        # front the same command works
        shell(f"am start -n {PACKAGE}/me.magnum.melonds.ui.romlist.RomListActivity")
        time.sleep(2.5)
    data = broadcast("LAUNCH_ROM", rom_uri=uri, wait_rom_ready=bool(a.get("wait", True)))
    return {"launched": uri, "reply": data, "foreground": foreground()}


def t_close(_):
    shell(f"am force-stop {PACKAGE}")
    return {"stopped": PACKAGE, "foreground": foreground()}


def t_screenshot(a):
    which = a.get("display", "both")
    width = int(a.get("max_width", 960))
    shots = []
    for name, extra in (("top", []), ("bottom", ["-d", BOTTOM_DISPLAY])):
        if which in (name, "both"):
            png = adb("exec-out", "screencap", "-p", *extra, binary=True, timeout=30)
            shots.append((name, shrink(png, width)))
    return {"_images": shots, "foreground": foreground()}


def shrink(png: bytes, width: int) -> bytes:
    try:
        from PIL import Image
    except ImportError:
        return png
    im = Image.open(io.BytesIO(png))
    if width and im.width > width:
        im = im.resize((width, round(im.height * width / im.width)))
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "PNG")
    return buf.getvalue()


def t_stats(a):
    seconds = float(a.get("seconds", 5))
    # read from a start time rather than clearing the log, which would lose other evidence
    start = shell("date '+%m-%d %H:%M:%S.000'").strip()
    time.sleep(seconds)
    lines = shell(f"logcat -d -T {shlex.quote(start)} -s melonDS:W", timeout=30).splitlines()
    keep = [l.split(": ", 1)[-1] for l in lines
            if any(t in l for t in ("HDTexPack", "VulkanOutput[Stats]", "VulkanPerf"))]
    fps = None
    try:
        fps = json.loads(broadcast("GET_FPS")).get("fps")
    except Exception:
        pass
    return {"seconds": seconds, "fps": fps, "lines": keep[-40:]}


def t_logcat(a):
    lines = int(a.get("lines", 200))
    text = shell(f"logcat -d -t {max(lines * 4, 400)}", timeout=30).splitlines()
    f = a.get("filter")
    if f:
        text = [l for l in text if f.lower() in l.lower()]
    return {"lines": text[-lines:]}


def t_pack(a):
    game = a.get("game", "").strip()
    action = a.get("action", "status")
    base = "files/texturepacks"
    run = lambda c: shell(f"run-as {PACKAGE} sh -c {shlex.quote(c)}", check=False)
    if action == "status":
        listing = run(f"for d in {base}/*; do echo \"$d $(ls $d/textures $d/sprites $d/bgtiles 2>/dev/null | wc -l)\"; done")
        return {"packs": [l for l in listing.splitlines() if l.strip()],
                "note": "a folder ending in .off is disabled; .bak-* folders are backups from push"}
    if not re.fullmatch(r"[A-Z0-9]{4}", game):
        raise RuntimeError("game must be a 4-character game code, e.g. BSDE")
    if action == "off":
        out = run(f"[ -d {base}/{game} ] && mv {base}/{game} {base}/{game}.off && echo off || echo 'no active pack'")
    elif action == "on":
        out = run(f"[ -d {base}/{game}.off ] && mv {base}/{game}.off {base}/{game} && echo on || echo 'no disabled pack'")
    else:
        raise RuntimeError("action must be status, on or off")
    return {"game": game, "result": out.strip(), "note": "packs load at game start: close and relaunch the game"}


def t_tap(a):
    display = {"top": "0", "bottom": "4"}.get(str(a.get("display", "top")), str(a.get("display")))
    shell(f"input -d {display} tap {int(a['x'])} {int(a['y'])}")
    return {"tapped": [a["x"], a["y"]], "display": display}


def t_key(a):
    shell(f"input keyevent {shlex.quote(str(a['key']))}")
    return {"key": a["key"]}


def t_save_state(a):
    return {"reply": broadcast("SAVE_STATE", slot=int(a.get("slot", 1)))}


def t_load_state(a):
    return {"reply": broadcast("LOAD_STATE", slot=int(a.get("slot", 1)))}


def prop(kind, desc):
    return {"type": kind, "description": desc}


TOOLS = {
    "status": (t_status, "Device and emulator state: what's in front (check before touching the "
               "Thor - it's shared), whether a game runs, FPS, and the latest HD pack and "
               "compositor stats.", {}),
    "settings_get": (t_settings_get, "Read the app's settings as JSON. `filter`: only keys "
                     "containing this, e.g. 'filter' or 'video'.", {"filter": prop("string", "key substring")}),
    "settings_set": (t_settings_set, "Set one setting; applied live to a running game. The stored "
                     "type decides parsing (e.g. video_hd_texture_filter is a string '0'-'13'). "
                     "`type` is only needed for a key that doesn't exist yet.",
                     {"key": prop("string", "preference key"), "value": prop("string", "new value"),
                      "type": prop("string", "boolean|int|long|float|set|string")}),
    "list_roms": (t_list_roms, "The ROM library (name, file, uri). Optional `query` substring.",
                  {"query": prop("string", "name or file substring")}),
    "launch": (t_launch, "Launch a game by `query` (name substring) or `uri`, and wait until it "
               "runs. Check `status` first: don't take the Thor from another session.",
               {"query": prop("string", "name substring"), "uri": prop("string", "ROM uri from list_roms"),
                "first": prop("boolean", "take the first of several matches"),
                "wait": prop("boolean", "wait for the ROM to start (default true)")}),
    "close": (t_close, "Close the emulator (force-stop). Do this when finished with the Thor.", {}),
    "screenshot": (t_screenshot, "Capture the top and/or bottom display as images. "
                   "`display`: top, bottom or both (default). `max_width` default 960 (0 = full).",
                   {"display": prop("string", "top|bottom|both"), "max_width": prop("integer", "shrink to this width")}),
    "stats": (t_stats, "Watch the game for `seconds` (default 5) and return the HD pack stats "
              "(hits/lookups per kind, 2D instances), compositor stats and FPS.",
              {"seconds": prop("number", "how long to sample")}),
    "logcat": (t_logcat, "Tail logcat. `lines` (default 200), optional `filter` substring.",
               {"lines": prop("integer", "lines"), "filter": prop("string", "substring")}),
    "pack": (t_pack, "HD packs on the device: `action` status (default), on or off for `game` "
             "(4-letter code). Off renames the folder so the pack isn't found; relaunch to apply.",
             {"game": prop("string", "game code, e.g. BSDE"), "action": prop("string", "status|on|off")}),
    "tap": (t_tap, "Tap the screen: `x`, `y` in display pixels, `display` top (default) or bottom. "
            "The bottom display is the DS touch screen.",
            {"x": prop("integer", "x"), "y": prop("integer", "y"), "display": prop("string", "top|bottom")}),
    "key": (t_key, "Send an Android key event, e.g. KEYCODE_BACK or KEYCODE_BUTTON_A.",
            {"key": prop("string", "keycode name")}),
    "save_state": (t_save_state, "Save the running game to state `slot` (default 1).",
                   {"slot": prop("integer", "slot")}),
    "load_state": (t_load_state, "Load state `slot` (default 1) into the running game.",
                   {"slot": prop("integer", "slot")}),
}


# ---------------------------------------------------------------------------- MCP over stdio

def reply(id_, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": id_}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def call(name, args):
    fn = TOOLS[name][0]
    try:
        out = fn(args or {})
    except Exception as e:
        return {"content": [{"type": "text", "text": f"error: {e}"}], "isError": True}
    content = []
    if isinstance(out, dict) and "_images" in out:
        for label, png in out.pop("_images"):
            content.append({"type": "text", "text": f"{label} display:"})
            content.append({"type": "image", "mimeType": "image/png", "data": base64.b64encode(png).decode()})
    content.insert(0, {"type": "text", "text": json.dumps(out, indent=1)})
    return {"content": content, "isError": isinstance(out, dict) and "error" in out}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method, id_ = msg.get("method"), msg.get("id")
        if id_ is None:
            continue   # notifications
        params = msg.get("params") or {}
        if method == "initialize":
            reply(id_, {"protocolVersion": params.get("protocolVersion", PROTOCOL),
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": "thor", "version": "1"},
                        "instructions": "Drive Watermelon Thor on the AYN Thor over adb. The Thor is shared "
                                        "with other sessions: call status first and don't take the device "
                                        "if another app is in front. Close the emulator when done."})
        elif method == "ping":
            reply(id_, {})
        elif method == "tools/list":
            reply(id_, {"tools": [{"name": n, "description": d,
                                   "inputSchema": {"type": "object", "properties": p}}
                                  for n, (_, d, p) in TOOLS.items()]})
        elif method == "tools/call":
            name = params.get("name")
            if name not in TOOLS:
                reply(id_, error={"code": -32602, "message": f"unknown tool {name}"})
            else:
                reply(id_, call(name, params.get("arguments")))
        else:
            reply(id_, error={"code": -32601, "message": f"method not found: {method}"})


if __name__ == "__main__":
    main()
