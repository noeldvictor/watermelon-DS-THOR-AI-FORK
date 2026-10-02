"""Smoke tests for the studio: a real server on a free port, adb replaced by a fake, and a fake
hd_remaster folder (a tool that only prints its arguments), so nothing touches the device or the
real pipeline.

    tools/hd_remaster/.venv/Scripts/python.exe -m unittest tools/studio/test_studio.py -v
"""
from __future__ import annotations

import contextlib
import http.client
import importlib.util
import io
import json
import os
import shutil
import struct
import sys
import tempfile
import threading
import time
import unittest
import zlib
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from app import StudioApp  # noqa: E402
from config import REPO_DIR, Settings  # noqa: E402
from jobs import JobManager  # noqa: E402
from pipeline import Library, parse_games_md  # noqa: E402
from server import make_server  # noqa: E402

SERIAL = "c3ca0370"
LAUNCHER = "com.android.launcher3/.uioverrides.QuickstepLauncher"
HERO = "Obj_hero.nsbmd__hero"
TREE = "Obj_tree.nsbmd__tree"
FAKE_KEY = "tsk-secret-777"
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

    # 3D models as `models extract` leaves them: a seen and edited model with AI references, an
    # unseen one, an index entry that tries to leave the folder, a ledger with 40 credits
    models = hd / "work" / "TSTE" / "models"
    for mid, name, seen, key in ((HERO, "hero", 12, "mdl1_64_00000000000000aa"), (TREE, "tree", 0, "mdl1_32_00000000000000bb")):
        folder = models / mid
        folder.mkdir(parents=True)
        (folder / "model.json").write_text(json.dumps({
            "source": f"Obj/{name}.nsbmd", "name": name, "id": mid, "bbox": [0, 0, 0, 1, 1, 1],
            "shapes": [{"key": key, "name": "body", "material": "skin", "texture_size": [32, 32], "lit": True,
                        "vertices": 30, "triangles": 10, "seen": seen}]}), encoding="utf-8")
        (folder / "model.obj").write_text("g body\n", encoding="utf-8")
        (folder / "tex_skin.png").write_bytes(tiny_png())
    hero = models / HERO
    (hero / "preview.png").write_bytes(tiny_png(16, 4))
    (hero / "edited.obj").write_text("g body\n", encoding="utf-8")
    (hero / "edited_preview.png").write_bytes(tiny_png(16, 4))
    (hero / "hidden.txt").write_text("eyes\n", encoding="utf-8")
    (hero / "ai").mkdir()
    for view in ("front", "right", "back", "left"):
        (hero / "ai" / f"ref_{view}.png").write_bytes(tiny_png())
    (hero / "ai" / "task1.glb").write_bytes(b"glTF")
    (hero / "ai" / "notes.txt").write_text("not a picture", encoding="utf-8")
    (models / "index.json").write_text(json.dumps([
        {"id": HERO, "source": "Obj/hero.nsbmd", "name": "hero", "shapes": 1, "triangles": 10, "seen": 12},
        {"id": TREE, "source": "Obj/tree.nsbmd", "name": "tree", "shapes": 1, "triangles": 10, "seen": 0},
        {"id": "../escape", "source": "x", "name": "bad", "shapes": 0, "triangles": 0, "seen": 0}]), encoding="utf-8")
    (models / "rom.txt").write_text(str(rom), encoding="utf-8")
    (models / "ai_ledger.jsonl").write_text('{"credits": 20, "provider": "tripo"}\n{"credits": 20}\nnot json\n\n',
                                            encoding="utf-8")
    (hd / "work" / "TSTE" / "secret.png").write_bytes(tiny_png())     # next to models/: never served
    built = hd / "work" / "TSTE" / "models_built"
    built.mkdir()
    (built / "mdl1_64_00000000000000aa.dl").write_bytes(b"\0" * 8)
    (built / "originals.txt").write_text("mdl1_64_00000000000000aa 00000000\n", encoding="utf-8")
    (hd / "packs" / "TSTE" / "models").mkdir()
    (hd / "packs" / "TSTE" / "models" / "mdl1_64_00000000000000aa.dl").write_bytes(b"\0" * 8)
    inputs = root / "inputs"
    inputs.mkdir()
    for name in ("new.glb", "new.OBJ", "notes.txt", "gx_cpu_words.bin"):
        (inputs / name).write_bytes(b"x")
    (inputs / "dl_trace.json").write_text(json.dumps({"lists": []}), encoding="utf-8")
    return {"hd": hd, "rom": rom, "roms": roms, "runs": root / "runs", "inputs": inputs}


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
        # a texture, a model replacement and pack.json
        self.assertEqual(pack["size_bytes"], 100 + 8 + len(json.dumps({"game": "TSTE", "scale": 4, "images": 3})))

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

    # ------------------------------------------------------------------ 3D models

    def run_step(self, step: str, options: dict, expect: int = 200) -> dict:
        return self.post_json("/api/games/TSTE/run", {"step": step, "options": options}, expect=expect)

    def test_models_list_detail_and_pictures(self) -> None:
        res = self.get_json("/api/games/TSTE/models3d")
        self.assertTrue(res["extracted"])
        self.assertEqual([m["id"] for m in res["models"]], [HERO, TREE])   # the escaping entry is dropped
        hero, tree = res["models"]
        self.assertEqual((hero["has_preview"], hero["has_edited"], hero["has_edited_preview"]), (True, True, True))
        self.assertEqual((hero["ai_refs"], hero["ai_meshes"], hero["seen"]), (["front", "right", "back", "left"], 1, 12))
        self.assertEqual((tree["has_preview"], tree["has_edited"], tree["ai_refs"]), (False, False, []))
        self.assertEqual(res["counts"], {"models": 2, "seen": 1, "edited": 1, "previews": 1})
        self.assertEqual((res["built"]["count"], res["pack"]["count"]), (1, 1))
        self.assertTrue(res["rom_exists"])
        self.assertEqual((res["ai"]["ledger_credits"], res["ai"]["ledger_calls"], res["ai"]["credits_per_call"]), (40, 2, 20))
        self.assertEqual(set(res["ai"]["keys"]), {"tripo", "meshy"})

        d = self.get_json(f"/api/games/TSTE/models3d/{HERO}")
        self.assertEqual((d["name"], d["triangles"], d["hidden"]), ("hero", 10, ["eyes"]))
        self.assertTrue(d["shapes"][0]["built"])
        self.assertFalse(self.get_json(f"/api/games/TSTE/models3d/{TREE}")["shapes"][0]["built"])
        self.assertTrue(d["edited"].endswith("edited.obj"))
        self.assertEqual(len(d["ai_meshes"]), 1)
        self.assertEqual([t["name"] for t in d["images"]["textures"]], ["skin"])
        urls = [d["images"]["preview"], d["images"]["edited_preview"], d["images"]["textures"][0]["url"]]
        urls += [r["url"] for r in d["images"]["ai_refs"]]
        self.assertEqual(len(urls), 7)
        for url in urls:
            status, headers, raw = self.request("GET", url)
            self.assertEqual((status, headers["Content-Type"]), (200, "image/png"), url)
            self.assertTrue(raw.startswith(b"\x89PNG"))
        self.assertIn("error", self.get_json("/api/games/TSTE/models3d/nope", expect=404))
        self.assertIn("error", self.get_json("/api/games/ZZZZ/models3d", expect=404))

        # only PNGs inside the model's folder and its ai/ folder
        base = f"/api/games/TSTE/models3d/{HERO}/file"
        for path in (f"{base}/model.json", f"{base}/ai/notes.txt", f"{base}/missing.png", f"{base}/../secret.png",
                     f"{base}/ai/../../secret.png", f"{base}/..%2Fsecret.png", "/api/games/TSTE/models3d/../file/secret.png",
                     "/api/games/TSTE/models3d/./file/secret.png", f"/api/games/TSTE/models3d/{HERO}/file/sub/x.png"):
            self.assertEqual(self.request("GET", path)[0], 404, path)
        lib = self.app.library
        self.assertIsNone(lib.model_file("TSTE", HERO, "../secret.png"))
        self.assertIsNone(lib.model_file("TSTE", "..", "secret.png"))
        self.assertIsNone(lib.model_file("TSTE", f"{HERO}/..", "preview.png"))
        self.assertIsNone(lib.model_file("tste", HERO, "preview.png"))
        self.assertIsNotNone(lib.model_file("TSTE", HERO, "ai/ref_front.png"))

    def test_models_extract_and_build_commands(self) -> None:
        rom, inp = str(self.paths["rom"]), self.paths["inputs"]
        trace, words = inp / "dl_trace.json", inp / "gx_cpu_words.bin"
        job = self.run_step("models_extract", {"rom_path": rom, "trace": f'"{trace}"', "cpu_words": str(words),
                                               "previews": True})["job"]
        self.assertIn(str(self.paths["hd"]), job["command"])      # the stand-in tool, never the real one
        lines, end = self.stream_job(job["id"])
        self.assertEqual((end["status"], end["title"], end["lane"]), ("done", "Extract models TSTE", "pipeline"), lines)
        self.assertIn(f"fake hd_remaster: models extract {rom} --trace {trace} --cpu-words {words} --previews", lines)
        err = self.run_step("models_extract", {"rom_path": rom, "trace": str(inp / "notes.txt")}, expect=400)
        self.assertIn(".json", err["error"])
        err = self.run_step("models_extract", {"rom_path": rom, "trace": str(inp / "gone.json")}, expect=400)
        self.assertIn("no file", err["error"])
        err = self.run_step("models_extract", {"rom_path": rom, "cpu_words": "gx_cpu_words.bin"}, expect=400)
        self.assertIn("full path", err["error"])

        job = self.run_step("models_build", {"smooth": "0.6", "only": "hero", "seen": True})["job"]
        lines, end = self.stream_job(job["id"])
        work = self.paths["hd"] / "work" / "TSTE"
        self.assertIn(f"fake hd_remaster: models build {work} --smooth 0.6 --only=hero --seen", lines)
        self.assertEqual(end["title"], "Build models TSTE (smooth 0.6, only 'hero', seen)")
        # without a smooth strength: the edited models (the hero has an edited.obj)
        job = self.run_step("models_build", {"smooth": "", "only": "", "seen": False})["job"]
        lines, _ = self.stream_job(job["id"])
        self.assertIn(f"fake hd_remaster: models build {work}", lines)
        for options, text in (({"smooth": "1.5"}, "0 to 1"), ({"smooth": "nan"}, "0 to 1"), ({"smooth": "soft"}, "0 to 1"),
                              ({"only": "hero"}, "smooth strength"), ({"seen": True}, "smooth strength")):
            self.assertIn(text, self.run_step("models_build", options, expect=400)["error"])

    def test_models_fit_commands(self) -> None:
        inp = self.paths["inputs"]
        job = self.run_step("models_fit", {"model": HERO, "mesh": str(inp / "new.glb")})["job"]
        lines, end = self.stream_job(job["id"])
        work = self.paths["hd"] / "work" / "TSTE"
        self.assertIn(f"fake hd_remaster: models fit {work} --model={HERO} --mesh {inp / 'new.glb'}", lines)
        self.assertEqual(end["title"], "Fit new.glb onto hero (TSTE)")
        self.assertIn("--mesh", self.run_step("models_fit", {"model": TREE, "mesh": str(inp / "new.OBJ")})["job"]["command"])
        for options, text in (({"model": HERO, "mesh": str(inp / "notes.txt")}, ".glb or .obj"),
                              ({"model": HERO, "mesh": str(inp / "gone.glb")}, "no file"),
                              ({"model": HERO, "mesh": "new.glb"}, "full path"),
                              ({"model": HERO, "mesh": ""}, "Paste the full path"),
                              ({"model": "nope", "mesh": str(inp / "new.glb")}, "no extracted model"),
                              ({"model": "..", "mesh": str(inp / "new.glb")}, "no extracted model")):
            self.assertIn(text, self.run_step("models_fit", options, expect=400)["error"])

    def test_models_ai_dry_run_needs_no_key(self) -> None:
        with mock.patch.dict(os.environ):
            os.environ.pop("TRIPO_API_KEY", None)
            os.environ.pop("MESHY_API_KEY", None)
            job = self.run_step("models_ai", {"model": HERO, "dry_run": True})["job"]
            lines, end = self.stream_job(job["id"])
            work = self.paths["hd"] / "work" / "TSTE"
            self.assertIn(f"fake hd_remaster: models ai {work} --model={HERO} --provider tripo --polycount 6000 "
                          "--budget 100 --dry-run", lines)
            self.assertEqual(end["title"], "AI dry run: hero (TSTE)")
            job = self.run_step("models_ai", {"model": HERO, "dry_run": True, "provider": "meshy", "polycount": "3000",
                                              "budget": 60})["job"]
            self.assertIn("--provider meshy --polycount 3000 --budget 60 --dry-run", job["command"])
            for options, text in (({"provider": "openai"}, "Tripo or Meshy"), ({"polycount": "lots"}, "polygon count"),
                                  ({"polycount": 50}, "polygon count"), ({"budget": -1}, "budget")):
                err = self.run_step("models_ai", {"model": HERO, "dry_run": True, **options}, expect=400)
                self.assertIn(text, err["error"])

    def test_models_ai_generate_needs_key_budget_and_confirmation(self) -> None:
        env = self.paths["hd"] / ".env"
        generate = {"model": HERO, "dry_run": False, "provider": "tripo"}
        with mock.patch.dict(os.environ):
            os.environ.pop("TRIPO_API_KEY", None)
            os.environ.pop("MESHY_API_KEY", None)
            before = len(self.app.jobs.list(kind="models_ai"))
            err = self.run_step("models_ai", {**generate, "confirm_spend": True}, expect=400)
            self.assertIn("No TRIPO_API_KEY is set", err["error"])
            self.assertFalse(self.get_json("/api/games/TSTE/models3d")["ai"]["keys"]["tripo"])
            env.write_text(f"OPENROUTER_API_KEY=\nTRIPO_API_KEY={FAKE_KEY}\n", encoding="utf-8")
            try:
                status, _, raw = self.request("GET", "/api/games/TSTE/models3d")
                self.assertTrue(json.loads(raw)["ai"]["keys"]["tripo"])
                self.assertFalse(json.loads(raw)["ai"]["keys"]["meshy"])
                self.assertTrue(self.get_json("/api/setup")["ai3d_keys"]["tripo"])
                err = self.run_step("models_ai", {**generate, "provider": "meshy", "confirm_spend": True}, expect=400)
                self.assertIn("MESHY_API_KEY", err["error"])
                # a key alone isn't enough: the client must confirm the cost explicitly
                for confirm in (None, False, "yes", 1):
                    err = self.run_step("models_ai", {**generate, "confirm_spend": confirm}, expect=400)
                    self.assertIn("spends 20 credits", err["error"])
                err = self.run_step("models_ai", {**generate, "confirm_spend": True, "budget": 50}, expect=400)
                self.assertIn("Raise the budget to at least 60", err["error"])
                self.assertEqual(len(self.app.jobs.list(kind="models_ai")), before, "no job for a refused call")

                res = self.run_step("models_ai", {**generate, "confirm_spend": True})
                job = res["job"]
                self.assertIn(str(self.paths["hd"]), job["command"])   # the stand-in tool prints, never calls out
                self.assertNotIn("--dry-run", job["command"])
                self.assertEqual(job["title"], "AI model: hero (TSTE, Tripo, 20 credits)")
                lines, end = self.stream_job(job["id"])
                self.assertEqual(end["status"], "done", lines)
                self.assertIn(f"fake hd_remaster: models ai {self.paths['hd'] / 'work' / 'TSTE'} --model={HERO} "
                              "--provider tripo --polycount 6000 --budget 100", lines)
                for path in ("/api/games/TSTE/models3d", f"/api/games/TSTE/models3d/{HERO}", "/api/setup", "/api/jobs",
                             f"/api/jobs/{job['id']}?tail=50"):
                    self.assertNotIn(FAKE_KEY.encode(), self.request("GET", path)[2], path)
                self.assertNotIn(FAKE_KEY, json.dumps(res))
            finally:
                env.unlink()

    def test_models_install_uses_the_device_guard(self) -> None:
        self.adb.foreground = "org.vita3k.emulator/.Emulator"
        try:
            err = self.run_step("models_push", {}, expect=409)
            self.assertTrue(err["needs_confirm"])
            self.assertEqual(self.app.jobs.list(kind="models_push"), [])
            job = self.post_json("/api/games/TSTE/run", {"step": "models_push", "options": {}, "confirm": True})["job"]
        finally:
            self.adb.foreground = LAUNCHER
        self.assertEqual((job["lane"], job["title"]), ("device", "Install models TSTE"))
        lines, end = self.stream_job(job["id"])
        self.assertEqual(end["status"], "done", lines)
        pack = self.paths["hd"] / "packs" / "TSTE"
        self.assertIn(f"fake hd_remaster: push {pack} --models-only --serial {SERIAL}", lines)
        models = pack / "models"
        models.rename(pack / "models_off")
        try:
            err = self.run_step("models_push", {}, expect=400)
            self.assertIn("Run Build models first", err["error"])
        finally:
            (pack / "models_off").rename(models)

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

    def test_repo_models_extraction(self) -> None:
        """A real `models extract` (work folders are local, never committed): index.json and
        model.json still have the fields the studio reads."""
        hd = REPO_DIR / "tools" / "hd_remaster"
        codes = sorted(p.parent.parent.name for p in (hd / "work").glob("*/models/index.json"))
        if not codes:
            self.skipTest("no models extraction in tools/hd_remaster/work")
        with tempfile.TemporaryDirectory() as tmp:
            lib = Library(Settings(Path(tmp) / "s.json"))
            listing = lib.model_list(codes[0])
            self.assertTrue(listing["models"], codes[0])
            first = listing["models"][0]
            self.assertTrue({"id", "source", "name", "shapes", "triangles", "seen"} <= set(first))
            detail = lib.model_detail(codes[0], first["id"])
            self.assertIsNotNone(detail)
            self.assertTrue(detail["shapes"][0]["key"].startswith("mdl1_"))
            self.assertTrue({"name", "material", "texture_size", "lit", "vertices", "triangles", "seen"}
                            <= set(detail["shapes"][0]))


class PushModelsOnlyTest(unittest.TestCase):
    """hd_remaster.py push --models-only with adb replaced: what it would run on the device."""

    def test_models_only_replaces_just_the_models_folder(self) -> None:
        hd_dir = REPO_DIR / "tools" / "hd_remaster"
        try:
            import numpy  # noqa: F401 - hd_remaster.py needs the pipeline's Python
            import PIL  # noqa: F401
        except ImportError:
            self.skipTest("numpy and Pillow are needed to import hd_remaster.py (run with its .venv)")
        saved_path = list(sys.path)
        try:
            spec = importlib.util.spec_from_file_location("hd_remaster_under_test", hd_dir / "hd_remaster.py")
            tool = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(tool)
        finally:
            sys.path[:] = saved_path
        calls: list[tuple[str | None, tuple[str, ...]]] = []

        def fake_adb(serial: str | None, *args: str, check: bool = True) -> str:
            calls.append((serial, args))
            return "1\n" if args[0] == "shell" and "run-as" in args[1] else ""

        with tempfile.TemporaryDirectory() as tmp:
            pack = Path(tmp) / "TSTE"
            (pack / "models").mkdir(parents=True)
            (pack / "models" / "mdl1_8_00000000000000aa.dl").write_bytes(b"\0" * 4)
            (pack / "textures").mkdir()
            argv = ["hd_remaster.py", "push", str(pack), "--models-only", "--serial", "X1"]
            out = io.StringIO()
            with mock.patch.object(tool, "adb", fake_adb), mock.patch.object(sys, "argv", argv), \
                    contextlib.redirect_stdout(out):
                tool.main()
            self.assertIn("installed files/texturepacks/TSTE/models (1 files)", out.getvalue())
            self.assertEqual({serial for serial, _ in calls}, {"X1"})
            pushes = [args for _, args in calls if args[0] == "push"]
            self.assertEqual(pushes, [("push", str(pack / "models"), "/data/local/tmp/hd_remaster_TSTE_models")])
            script = next(args[1] for _, args in calls if args[0] == "shell" and "run-as" in args[1])
            self.assertIn("rm -rf files/texturepacks/TSTE/models && "
                          "cp -r /data/local/tmp/hd_remaster_TSTE_models files/texturepacks/TSTE/models", script)
            self.assertNotIn(".bak", script)       # no backup, and the rest of the pack is left alone

            # nothing built: refused before adb is touched
            calls.clear()
            empty = Path(tmp) / "EMPT"
            empty.mkdir()
            with mock.patch.object(tool, "adb", fake_adb), self.assertRaises(SystemExit):
                tool.push_models(empty, "EMPT", "X1")
            self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
