"""Smoke tests for the studio: a real server on a free port, adb replaced by a fake, and a fake
hd_remaster folder (a tool that only prints its arguments), so nothing touches the device or the
real pipeline.

    tools/hd_remaster/.venv/Scripts/python.exe -m unittest tools/studio/test_studio.py -v
"""
from __future__ import annotations

import http.client
import json
import shutil
import struct
import sys
import tempfile
import threading
import time
import unittest
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from app import StudioApp  # noqa: E402
from config import REPO_DIR, Settings  # noqa: E402
from jobs import JobManager  # noqa: E402
from pipeline import Library, parse_games_md  # noqa: E402
from server import make_server  # noqa: E402

SERIAL = "c3ca0370"
LAUNCHER = "com.android.launcher3/.uioverrides.QuickstepLauncher"
FAKE_TOOL = r'''
import sys, time
args = sys.argv[1:]
print("fake hd_remaster:", " ".join(args), flush=True)
if "--only" in args and args[args.index("--only") + 1] == "slow":
    for i in range(300):
        print(f"working {i}", flush=True)
        time.sleep(0.1)
print("done in 0s", flush=True)
'''


def tiny_png(w: int = 4, h: int = 2) -> bytes:
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * w for _ in range(h))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class FakeAdb:
    """Answers the adb commands the studio sends, like an idle Thor with our app installed."""

    def __init__(self) -> None:
        self.foreground = LAUNCHER
        self.app_running = False
        self.calls: list[list[str]] = []
        self.lock = threading.Lock()

    def __call__(self, cmd: list[str], timeout: float) -> tuple[int, bytes, bytes]:
        with self.lock:
            self.calls.append(cmd)
        if cmd[:3] == ["adb", "devices", "-l"]:
            return 0, (f"List of devices attached\n{SERIAL}               device product:kalama "
                       "model:AYN_Thor device:kalama transport_id:11\n\n").encode(), b""
        assert cmd[:3] == ["adb", "-s", SERIAL], cmd
        rest = cmd[3:]
        if rest[:3] == ["exec-out", "screencap", "-p"]:
            return 0, tiny_png(), b""
        if rest[0] == "shell":
            text = rest[1]
            if "dumpsys activity" in text:
                out = (f"      topResumedActivity=ActivityRecord{{b590053 u0 {self.foreground}}} t4244}}\n"
                       f"  ResumedActivity: ActivityRecord{{b590053 u0 {self.foreground}}} t4244}}\n")
                if "pidof" in text:
                    out += "@@\n" + ("12345\n" if self.app_running else "") + "@@\n  mWakefulness=Awake\n"
                return 0, out.encode(), b""
            if text.startswith("am broadcast"):
                if "GET_FPS" in text:
                    data = '{"fps":59.8,"running":true}'
                elif "GET_PREFERENCES" in text:
                    data = '{"enable_texture_packs":true,"video_renderer":"vulkan"}'
                elif "LIST_ROMS" in text:
                    data = '[{"name":"Lufia","file":"Lufia.7z","uri":"content://x/Lufia.7z"}]'
                elif "SET_PREFERENCE" in text:
                    data = '{"key":"enable_texture_packs","new":false,"appliedToRunningGame":true}'
                else:
                    data = "success=1"
                return 0, f'Broadcasting: Intent {{ flg=0x400020 }}\nBroadcast completed: result=1, data="{data}"\n'.encode(), b""
            if "run-as" in text and "ls files" in text:
                return 0, b"a.ml\nb.ml\nnotes.txt\n", b""
            return 0, b"", b""
        return 0, b"", b""

    def sent(self, needle: str) -> bool:
        with self.lock:
            return any(needle in " ".join(c) for c in self.calls)


def make_fixture(root: Path) -> dict[str, Path]:
    """A fake tools/hd_remaster with one game (TSTE), a ROM, a pack and one checks run."""
    hd = root / "hd_remaster"
    (hd / "games" / "TSTE" / "media").mkdir(parents=True)
    (hd / "hd_remaster.py").write_text(FAKE_TOOL, encoding="utf-8")
    (hd / "setup.ps1").write_text("Write-Host setup", encoding="utf-8")
    (hd / "models.json").write_text(json.dumps({"4x-UltraSharp": {}, "RealESRGAN_x4plus_anime_6B": {}}), encoding="utf-8")
    (hd / "games" / "TSTE" / "recipe.json").write_text(json.dumps({
        "game": "TSTE", "title": "Test Game", "scale": 4, "models": {"textures": "4x-UltraSharp"},
        "baseline": {"textures": 1}, "notes": ["a note"]}), encoding="utf-8")
    (hd / "games" / "TSTE" / "media" / "title.jpg").write_bytes(b"\xff\xd8\xff\xe0fakejpeg")
    (hd / "games" / "GAMES.md").write_text(
        "# Games\n\n## Recipes\n\n| | Game | Status | Pack | Build time | Coverage |\n| --- | --- | --- | --- | --- | --- |\n"
        '| [<img src="TSTE/media/title.jpg" width="260" alt="x">](TSTE/README.md) | [Test Game](TSTE/README.md)<br>TSTE, USA '
        "| **In progress** | 1 MB<br>3 images | ~1 min | Everything [how](x.md). |\n\n## Wishlist\n", encoding="utf-8")
    (hd / "work" / "TSTE").mkdir(parents=True)
    (hd / "work" / "TSTE" / "manifest.jsonl").write_text("{}\n", encoding="utf-8")
    (hd / "packs" / "TSTE" / "textures").mkdir(parents=True)
    (hd / "packs" / "TSTE" / "pack.json").write_text(json.dumps({"game": "TSTE", "scale": 4, "images": 3}), encoding="utf-8")
    (hd / "packs" / "TSTE" / "textures" / "a.png").write_bytes(b"x" * 100)
    roms = root / "roms"
    roms.mkdir()
    rom = roms / "test.nds"
    rom.write_bytes(b"TESTGAME\0\0\0\0" + b"TSTE" + b"\0" * 0x1F0)
    (roms / "notarom.nds").write_bytes(b"short")
    case = root / "runs" / "20260101_000000" / "Test_case_slot_1"
    case.mkdir(parents=True)
    (case / "report.json").write_text(json.dumps({
        "rom": "Test.7z", "state": "slot:1", "frames": 120, "baselineChanges": None,
        "rows": [{"frame": 3, "screen": "top", "share": 0.2, "flicker": 4, "flagged": True},
                 {"frame": 3, "screen": "bottom", "share": 0.01, "flicker": 0}]}), encoding="utf-8")
    (case / "Test_case_slot_1_frame0003_top.png").write_bytes(tiny_png())
    return {"hd": hd, "rom": rom, "roms": roms, "runs": root / "runs"}


class StudioServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="studio_test_"))
        cls.paths = make_fixture(cls.tmp)
        settings_path = cls.tmp / "studio_settings.json"
        settings_path.write_text(json.dumps({
            "hd_remaster_dir": str(cls.paths["hd"]), "python": sys.executable,
            "rom_dirs": [str(cls.paths["roms"])], "checks_out_dir": str(cls.paths["runs"]),
            "checks_baseline_roots": [], "game_roms": {},
        }), encoding="utf-8")
        cls.adb = FakeAdb()
        cls.app = StudioApp(settings_path, adb_runner=cls.adb, jobs=JobManager(default_grace=3.0))
        cls.server = make_server(cls.app, "127.0.0.1", 0)
        cls.port = cls.server.port
        cls.thread = threading.Thread(target=cls.server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.app.jobs.shutdown(max_grace=2)
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ------------------------------------------------------------------ helpers

    def request(self, method: str, path: str, body=None, headers=None) -> tuple[int, dict, bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        hdrs = dict(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            hdrs.setdefault("Content-Type", "application/json")
        conn.request(method, path, body=data, headers=hdrs)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        return resp.status, dict(resp.getheaders()), raw

    def get_json(self, path: str, expect: int = 200) -> dict:
        status, headers, raw = self.request("GET", path)
        self.assertEqual(status, expect, raw[:300])
        self.assertIn("application/json", headers["Content-Type"])
        return json.loads(raw)

    def post_json(self, path: str, body: dict, expect: int = 200) -> dict:
        status, headers, raw = self.request("POST", path, body)
        self.assertEqual(status, expect, raw[:300])
        return json.loads(raw)

    def stream_job(self, job_id: str, timeout: float = 30) -> tuple[list[str], dict]:
        """Read a job's SSE stream to its 'end' event; returns (lines, final summary)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        conn.request("GET", f"/api/jobs/{job_id}/events")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertIn("text/event-stream", resp.getheader("Content-Type"))
        lines, event, end = [], None, None
        deadline = time.time() + timeout
        while time.time() < deadline:
            raw = resp.fp.readline()
            if not raw:
                break
            text = raw.decode().rstrip("\n")
            if text.startswith("event: "):
                event = text[7:]
            elif text.startswith("data: "):
                data = json.loads(text[6:])
                if event == "lines":
                    lines += data["lines"]
                elif event == "end":
                    end = data
                    break
        conn.close()
        self.assertIsNotNone(end, f"no end event; got {lines[-5:]}")
        return lines, end

    # ------------------------------------------------------------------ pages and static files

    def test_index_and_static(self) -> None:
        status, headers, raw = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Remaster Studio", raw)
        status, headers, _ = self.request("GET", "/static/js/main.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", headers["Content-Type"])
        status, _, _ = self.request("GET", "/static/../studio.py")
        self.assertEqual(status, 404)
        self.assertTrue(self.get_json("/api/health")["ok"])

    # ------------------------------------------------------------------ library

    def test_games_list_and_detail(self) -> None:
        games = self.get_json("/api/games")["games"]
        tste = next(g for g in games if g["code"] == "TSTE")
        self.assertEqual(tste["title"], "Test Game")
        self.assertEqual(tste["status"], "In progress")
        self.assertEqual(tste["image"], "/api/games/TSTE/media/title.jpg")
        self.assertEqual(tste["pack"]["images"], 3)
        self.assertTrue(tste["stages"]["extracted"])
        detail = self.get_json("/api/games/TSTE")
        self.assertEqual(detail["recipe"]["title"], "Test Game")
        self.assertIn("RealESRGAN_x4plus_anime_6B", detail["models"])
        if not detail["rom"]["path"]:   # before test_new_game remembers it: offered from the ROM folder
            self.assertEqual(detail["rom"]["suggestions"], [str(self.paths["rom"])])
        status, headers, _ = self.request("GET", "/api/games/TSTE/media/title.jpg")
        self.assertEqual((status, headers["Content-Type"]), (200, "image/jpeg"))
        self.assertIn("error", self.get_json("/api/games/ZZZZ", expect=404))
        # the pack size is measured in the background
        for _ in range(50):
            pack = self.get_json("/api/games/TSTE")["pack"]
            if pack["size_bytes"] is not None:
                break
            time.sleep(0.05)
        self.assertEqual(pack["size_bytes"], 100 + len(json.dumps({"game": "TSTE", "scale": 4, "images": 3})))

    def test_setup_never_reveals_the_key(self) -> None:
        env = self.paths["hd"] / ".env"
        self.assertFalse(self.get_json("/api/setup")["openrouter_key_set"])
        env.write_text("OPENROUTER_API_KEY=sk-or-secret-123\n", encoding="utf-8")
        try:
            status, _, raw = self.request("GET", "/api/setup")
            self.assertTrue(json.loads(raw)["openrouter_key_set"])
            self.assertNotIn(b"sk-or-secret-123", raw)
            for path in ("/api/settings", "/api/games", "/api/games/TSTE"):
                self.assertNotIn(b"sk-or-secret", self.request("GET", path)[2])
        finally:
            env.unlink()

    def test_rom_scan(self) -> None:
        roms = self.get_json("/api/roms")["roms"]
        self.assertEqual([(r["code"], r["title"]) for r in roms], [("TSTE", "TESTGAME")])

    def test_new_game_runs_extract_and_streams(self) -> None:
        res = self.post_json("/api/games/new", {"rom_path": f'"{self.paths["rom"]}"'})
        self.assertEqual(res["code"], "TSTE")
        self.assertIsNotNone(res["job"])
        lines, end = self.stream_job(res["job"]["id"])
        self.assertEqual(end["status"], "done", lines)
        self.assertTrue(any(line.startswith("fake hd_remaster: extract") and "test.nds" in line for line in lines), lines)
        jobs = self.get_json("/api/jobs?game=TSTE")["jobs"]
        self.assertIn(res["job"]["id"], [j["id"] for j in jobs])
        # the ROM is remembered for the next steps
        self.assertTrue(self.get_json("/api/games/TSTE")["rom"]["code_matches"])

    def test_bad_inputs_get_clear_errors(self) -> None:
        err = self.post_json("/api/games/new", {"rom_path": str(self.tmp / "missing.nds")}, expect=400)
        self.assertIn("no file", err["error"])
        err = self.post_json("/api/games/new", {"rom_path": str(self.paths["roms"] / "notarom.nds")}, expect=400)
        self.assertIn("too small", err["error"])
        self.post_json("/api/games/TSTE/run", {"step": "bogus"}, expect=400)
        err = self.post_json("/api/games/TSTE/run", {"step": "verify", "options": {"dumps": str(self.tmp)}}, expect=400)
        self.assertIn("manifest.jsonl", err["error"])
        err = self.post_json("/api/games/TSTE/run", {"step": "upscale", "options": {"model": "nope"}}, expect=400)
        self.assertIn("Unknown model", err["error"])

    def test_cancel_stops_a_running_job(self) -> None:
        job = self.post_json("/api/games/TSTE/run", {"step": "upscale", "options": {"only": "slow", "scale": "2"}})["job"]
        self.assertIn("--scale 2", job["command"])
        for _ in range(100):
            if self.get_json(f"/api/jobs/{job['id']}?tail=5")["job"]["lines"] >= 3:
                break
            time.sleep(0.05)
        self.post_json(f"/api/jobs/{job['id']}/cancel", {})
        lines, end = self.stream_job(job["id"], timeout=20)
        self.assertEqual(end["status"], "cancelled")
        self.assertLess(len([line for line in lines if line.startswith("working")]), 250)

    # ------------------------------------------------------------------ device

    def test_device_status_and_screens(self) -> None:
        self.adb.app_running = False
        self.app.device.invalidate()
        s = self.get_json("/api/device/status")
        self.assertTrue(s["connected"])
        self.assertEqual(s["model"], "AYN Thor")
        self.assertEqual(s["foreground"]["kind"], "launcher")
        self.assertIsNone(s["fps"])
        self.assertFalse(self.adb.sent("GET_FPS"), "no broadcast while the app is stopped")
        self.adb.app_running = True
        self.app.device.invalidate()
        s = self.get_json("/api/device/status")
        self.assertEqual(s["fps"], 59.8)
        self.assertTrue(s["texture_packs"])
        self.adb.app_running = False
        for which in ("top", "bottom"):
            status, headers, raw = self.request("GET", f"/api/device/screen/{which}")
            self.assertEqual(status, 200)
            self.assertIn(headers["Content-Type"], ("image/jpeg", "image/png"))
        self.assertTrue(self.adb.sent("screencap -p -d 4630946482288158084"))
        self.assertEqual(self.get_json("/api/device/states")["states"], ["a.ml", "b.ml"])
        self.assertEqual(self.get_json("/api/device/roms")["roms"][0]["name"], "Lufia")

    def test_device_commands_ask_when_another_app_is_in_front(self) -> None:
        self.adb.foreground = "org.vita3k.emulator/.Emulator"
        try:
            err = self.post_json("/api/device/close", {}, expect=409)
            self.assertTrue(err["needs_confirm"])
            self.assertEqual(err["foreground"]["kind"], "other")
            self.assertFalse(self.adb.sent("am force-stop"))
            self.post_json("/api/device/close", {"confirm": True})
            self.assertTrue(self.adb.sent("am force-stop app.watermelonthor.dev"))
        finally:
            self.adb.foreground = LAUNCHER

    def test_states_and_texture_packs(self) -> None:
        self.post_json("/api/device/save_state", {"name": "bad name!"}, expect=400)
        res = self.post_json("/api/device/save_state", {"name": "title_screen"})
        self.assertEqual(res["saved"], "/data/user/0/app.watermelonthor.dev/files/title_screen.ml")
        self.assertTrue(self.adb.sent("--es path /data/user/0/app.watermelonthor.dev/files/title_screen.ml"))
        self.post_json("/api/device/load_state", {"name": "title_screen.ml"})
        res = self.post_json("/api/device/texture_packs", {"on": False})
        self.assertTrue(res["appliedToRunningGame"])
        self.assertTrue(self.adb.sent("--es key enable_texture_packs --es value false --es type boolean"))
        self.post_json("/api/device/launch", {"file": "Lufia.7z"})
        self.assertTrue(self.adb.sent("2664-21DE%3ARoms%2Fnds%2FLufia.7z"))

    # ------------------------------------------------------------------ checks

    def test_checks_runs_and_strips(self) -> None:
        runs = self.get_json("/api/checks/runs")["runs"]
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["strips"], 1)
        detail = self.get_json(f"/api/checks/run?id={runs[0]['id']}")
        strip = detail["cases"][0]["strips"][0]
        self.assertEqual((strip["frame"], strip["screen"], strip["info"]["share"]), (3, "top", 0.2))
        status, headers, _ = self.request("GET", f"/api/checks/file?id={strip['file']}")
        self.assertEqual((status, headers["Content-Type"]), (200, "image/png"))
        self.assertEqual(self.request("GET", "/api/checks/file?id=0/../../studio_settings.json")[0], 404)
        files = self.get_json("/api/checks/cases")["files"]
        cases = next(f for f in files if f["name"] == "cases_thor.txt")
        self.assertTrue(cases["cases"])
        # with a game in front a check must not start (it may be another session's game); this
        # never reaches the point where frame_compare would run against the real adb
        self.adb.foreground = "app.watermelonthor.dev/me.magnum.melonds.ui.emulator.EmulatorActivity"
        try:
            err = self.post_json("/api/checks/run", {"options": {"cases": cases["path"]}}, expect=400)
            self.assertIn("close the emulator", err["error"])
        finally:
            self.adb.foreground = LAUNCHER
        self.assertEqual(self.app.jobs.list(kind="checks"), [])

    # ------------------------------------------------------------------ settings and request safety

    def test_settings_validation(self) -> None:
        self.post_json("/api/settings", {"settings": {"serial": "bad serial!"}}, expect=400)
        self.post_json("/api/settings", {"settings": {"nonsense": 1}}, expect=400)
        res = self.post_json("/api/settings", {"settings": {"bottom_display": "4630946482288158084"}})
        self.assertEqual(res["settings"]["bottom_display"], "4630946482288158084")
        self.assertTrue(json.loads(Path(res["file"]).read_text(encoding="utf-8"))["rom_dirs"])

    def test_requests_from_other_sites_are_refused(self) -> None:
        status, _, _ = self.request("POST", "/api/device/close", {}, {"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        status, _, _ = self.request("POST", "/api/device/close", {}, {"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(status, 403)
        status, _, _ = self.request("POST", "/api/device/close", {}, {"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        status, _, _ = self.request("GET", "/api/games", headers={"Host": "evil.example"})
        self.assertEqual(status, 403)


class RealLibraryReadOnlyTest(unittest.TestCase):
    """Reads the repo's own recipes and GAMES.md (no writes) to catch format drift."""

    def test_repo_recipes_and_games_md(self) -> None:
        hd = REPO_DIR / "tools" / "hd_remaster"
        if not (hd / "games" / "GAMES.md").exists():
            self.skipTest("no tools/hd_remaster/games in this checkout")
        rows = parse_games_md((hd / "games" / "GAMES.md").read_text(encoding="utf-8"))
        self.assertIn("BSDE", rows)
        self.assertTrue(rows["BSDE"]["status"].startswith("In progress"))
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(Path(tmp) / "s.json")
            lib = Library(settings)
            codes = lib.codes()
            self.assertTrue({"BSDE", "AZEE"} <= set(codes), codes)
            summary = lib.summary("BSDE")
            self.assertEqual(summary["title"], "Lufia: Curse of the Sinistrals")


if __name__ == "__main__":
    unittest.main(verbosity=2)
