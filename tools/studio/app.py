"""StudioApp: the operations behind the HTTP API, independent of HTTP.

Each method returns plain JSON-able data or raises one of the user-facing errors
(PipelineError, ChecksError, SettingsError -> 400; NeedsConfirm -> 409; DeviceError -> 503;
NotFound -> 404). server.py maps those to responses.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from checks import Checks, ChecksError
from config import Settings, adb_available
from device import Device, Runner, default_runner
from jobs import Job, JobManager
from pipeline import CODE, DEVICE_STEPS, Library, PipelineError, clean_path, read_rom_header, scan_rom_dirs


class NotFound(LookupError):
    """Unknown game, job or file."""


class StudioApp:
    def __init__(self, settings_path: Path | str | None = None, adb_runner: Runner = default_runner,
                 jobs: JobManager | None = None):
        self.settings = Settings(settings_path) if settings_path else Settings()
        self.jobs = jobs or JobManager()
        self.device = Device(self.settings, adb_runner)
        self.library = Library(self.settings)
        self.checks = Checks(self.settings, self.device)

    # ------------------------------------------------------------------ library

    def games(self) -> dict[str, Any]:
        md = self.library.games_md()
        games = [self.library.summary(code, md) for code in self.library.codes()]
        return {"games": games, "hd_remaster_dir": str(self.library.hd)}

    def setup_status(self) -> dict[str, Any]:
        status = self.library.setup_status()
        status["adb_found"] = adb_available()
        running = [j.summary() for j in self.jobs.list(kind="setup") if not j.finished]
        status["job"] = running[0] if running else None
        return status

    def roms_found(self) -> dict[str, Any]:
        return {"roms": scan_rom_dirs(self.settings), "rom_dirs": self.settings.get("rom_dirs") or []}

    def _known_game(self, code: str) -> None:
        if not CODE.match(code or ""):
            raise NotFound(f"'{code}' is not a game code")
        if code not in self.library.codes():
            raise NotFound(f"No game {code} in the library")

    def game(self, code: str) -> dict[str, Any]:
        self._known_game(code)
        detail = self.library.detail(code)
        detail["jobs"] = [j.summary() for j in self.jobs.list(game=code)]
        return detail

    def models3d(self, code: str) -> dict[str, Any]:
        """The game's extracted 3D models, built replacements and AI ledger (Game page, 3D models)."""
        self._known_game(code)
        return self.library.model_list(code)

    def model3d(self, code: str, model_id: str) -> dict[str, Any]:
        self._known_game(code)
        detail = self.library.model_detail(code, model_id)
        if detail is None:
            raise NotFound(f"No extracted model '{model_id}' for {code}. Run Extract models, or reload the list.")
        return detail

    def _rom_from(self, rom_path: str) -> tuple[Path, dict[str, str]]:
        p = clean_path(rom_path)
        if not str(rom_path or "").strip():
            raise PipelineError("Paste the full path of a .nds file, e.g. C:\\ROMs\\game.nds")
        if not p.is_absolute():
            raise PipelineError("Use the full path of the ROM file, starting with the drive letter.")
        if not p.is_file():
            raise PipelineError(f"There is no file at {p}.")
        return p, read_rom_header(p)

    def new_game(self, rom_path: str, start_extract: bool = True) -> dict[str, Any]:
        """Register a ROM as a game project and (by default) start its extraction."""
        p, head = self._rom_from(rom_path)
        code = head["code"]
        self.settings.remember_rom(code, str(p), head["title"])
        result: dict[str, Any] = {"code": code, "title": head["title"], "path": str(p),
                                  "has_recipe": self.library.recipe(code) is not None, "job": None}
        if start_extract:
            try:
                result["job"] = self.run_step(code, "extract", {}, confirm=True)
            except PipelineError as e:
                # the project exists now; the game page shows what to do before extracting
                result["error"] = str(e)
        return result

    def set_rom(self, code: str, rom_path: str) -> dict[str, Any]:
        p, head = self._rom_from(rom_path)
        if head["code"] != code:
            raise PipelineError(f"{p.name} is game {head['code']}, not {code}. Pick the right ROM.")
        self.settings.remember_rom(code, str(p), head["title"])
        return {"code": code, "path": str(p), "title": head["title"]}

    def run_step(self, code: str, step: str, options: dict[str, Any], confirm: bool = False) -> dict[str, Any]:
        title, cmd = self.library.step_command(code, step, options or {})
        lane = "pipeline"
        if step in DEVICE_STEPS:
            self.device.guard(confirm)
            lane = "device"
        job = self.jobs.submit(title, cmd, kind=step, lane=lane, game=code, cwd=str(self.library.hd))
        return job.summary()

    def run_setup(self) -> dict[str, Any]:
        cmd = self.library.setup_command()
        job = self.jobs.submit("Setup: Python, PyTorch and the upscale model", cmd, kind="setup",
                               lane="pipeline", cwd=str(self.library.hd), grace=5.0)
        return job.summary()

    # ------------------------------------------------------------------ jobs

    def job(self, job_id: str) -> Job:
        job = self.jobs.get(job_id)
        if job is None:
            raise NotFound("That job isn't known (the studio was restarted?)")
        return job

    def job_list(self, game: str | None = None, kind: str | None = None) -> dict[str, Any]:
        return {"jobs": [j.summary() for j in self.jobs.list(game=game, kind=kind)]}

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        self.job(job_id)
        job = self.jobs.cancel(job_id)
        return job.summary() if job else {}

    # ------------------------------------------------------------------ device

    def device_action(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        confirm = bool(body.get("confirm"))
        d = self.device
        if action == "launch":
            uri = str(body.get("uri") or "").strip()
            file_name = str(body.get("file") or "").strip()
            if not uri and not file_name:
                raise PipelineError("Pick a game from the Thor's ROM list, or type its file name.")
            if uri and not uri.startswith("content://") and not uri.startswith("file://"):
                raise PipelineError("That ROM address isn't a content:// URI from the ROM list.")
            d.guard(confirm)
            return d.launch(uri or d.rom_uri(file_name))
        if action == "save_state":
            d.state_path(str(body.get("name") or ""))      # validate before touching the device
            d.guard(confirm)
            return d.save_state(str(body.get("name")))
        if action == "load_state":
            d.state_path(str(body.get("name") or ""))
            d.guard(confirm)
            return d.load_state(str(body.get("name")))
        if action == "texture_packs":
            if not isinstance(body.get("on"), bool):
                raise PipelineError("Say on or off.")
            d.guard(confirm)
            return d.set_texture_packs(body["on"])
        if action == "close":
            d.guard(confirm)
            return d.close_emulator()
        raise NotFound(f"unknown device action {action}")

    # ------------------------------------------------------------------ checks

    def run_checks(self, options: dict[str, Any], confirm: bool = False) -> dict[str, Any]:
        title, cmd, meta = self.checks.command(options or {})
        fg = self.device.guard(confirm)
        if fg["kind"] == "emulator":
            # frame_compare refuses too: it launches each game itself, and a running game may
            # be another session's
            raise ChecksError("A game is running on the Thor. Checks launch their games themselves: close the "
                              "emulator first (Thor panel, Close emulator) if nobody else is using it.")
        job = self.jobs.submit(title, cmd, kind="checks", lane="device", cwd=str(self.checks.tool.parent),
                               meta=meta, grace=60.0, prepare=self.checks.snapshot_prefs,
                               after_kill=self.checks.restore_prefs)
        return job.summary()
