"""Renderer checks: tools/frame_compare/frame_compare.py runs, their reports and flagged strips.

A run folder (frame_compare's --out/<timestamp>) holds one folder per case; a case folder holds
report.json, the pulled frame sequences (<case>_vulkan/, <case>_software/) and one PNG strip per
flagged frame at its top level:

    <case>_frame0012_top.png            Vulkan | software i | software i+1 | differing pixels
    <case>_baseline_frame0012_top.png   new | baseline | software | changed pixels
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from config import RUNNER, Settings
from device import Device, DeviceError
from jobs import Job

STRIP = re.compile(r"_(baseline_)?frame(\d{4})_(top|bottom)\.png$")
# the preferences frame_compare changes during a run (and restores at the end)
CHANGED_PREFS = ("video_renderer", "video_internal_resolution", "enable_texture_packs",
                 "video_renderer_debug_tools_enabled")


class ChecksError(ValueError):
    """A check can't run as asked; the message says what to fix."""


def parse_cases(text: str) -> list[dict[str, Any]]:
    cases = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2:
            continue
        cases.append({"rom": parts[0], "state": parts[1], "frames": parts[2] if len(parts) > 2 else None})
    return cases


class Checks:
    def __init__(self, settings: Settings, device: Device):
        self.settings = settings
        self.device = device

    @property
    def tool(self) -> Path:
        return self.settings.frame_compare_dir() / "frame_compare.py"

    def roots(self) -> list[Path]:
        """Folders whose subfolders are runs: our output folder first, then baseline folders."""
        out = [self.settings.checks_out_dir()]
        for r in self.settings.get("checks_baseline_roots") or []:
            p = Path(r)
            if p not in out:
                out.append(p)
        return out

    def case_files(self) -> list[dict[str, Any]]:
        files = []
        d = self.settings.frame_compare_dir()
        if d.is_dir():
            for p in sorted(d.glob("*.txt")):
                try:
                    cases = parse_cases(p.read_text(encoding="utf-8"))
                except OSError:
                    continue
                files.append({"path": str(p), "name": p.name, "cases": cases})
        return files

    # ------------------------------------------------------------------ runs and strips

    def runs(self) -> list[dict[str, Any]]:
        runs = []
        for root in self.roots():
            if not root.is_dir():
                continue
            for run in root.iterdir():
                if not run.is_dir():
                    continue
                cases = [c for c in run.iterdir() if c.is_dir() and (c / "report.json").exists()]
                if not cases and not any(run.iterdir()):
                    continue
                flagged = 0
                for c in cases:
                    flagged += len(list(c.glob("*_frame????_*.png")))
                runs.append({
                    "id": self._rel(run), "name": run.name, "root": str(root), "path": str(run),
                    "is_output": root == self.settings.checks_out_dir(),
                    "cases": len(cases), "strips": flagged, "modified": run.stat().st_mtime,
                })
        return sorted(runs, key=lambda r: r["modified"], reverse=True)

    def run_detail(self, run_id: str) -> dict[str, Any]:
        run = self.resolve(run_id)
        if run is None or not run.is_dir():
            raise ChecksError("That run folder doesn't exist any more.")
        cases = []
        for c in sorted(run.iterdir()):
            if not c.is_dir():
                continue
            report_path = c / "report.json"
            report: dict[str, Any] = {}
            if report_path.exists():
                try:
                    report = json.loads(report_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    report = {}
            rows = report.get("rows") or []
            by_frame = {(r.get("frame"), r.get("screen")): r for r in rows}
            changes = {(c2.get("frame"), c2.get("screen")): c2 for c2 in (report.get("baselineChanges") or [])}
            strips = []
            for png in sorted(c.glob("*.png")):
                m = STRIP.search(png.name)
                if not m:
                    continue
                frame, screen, baseline = int(m.group(2)), m.group(3), bool(m.group(1))
                row = changes.get((frame, screen)) if baseline else by_frame.get((frame, screen))
                strips.append({"file": self._rel(png), "name": png.name, "frame": frame, "screen": screen,
                               "baseline": baseline, "info": row or {}})
            flagged = [r for r in rows if r.get("flagged")]
            shares = {s: [r["share"] for r in rows if r.get("screen") == s and "share" in r] for s in ("top", "bottom")}
            usual = {s: (sorted(v)[len(v) // 2] if v else None) for s, v in shares.items()}
            cases.append({
                "name": c.name, "rom": report.get("rom"), "state": report.get("state"),
                "frames": report.get("frames"), "has_report": bool(report), "flagged": len(flagged),
                "usual_share": usual,
                "baseline_changes": None if report.get("baselineChanges") is None else len(report["baselineChanges"]),
                "worse": sum(1 for ch in (report.get("baselineChanges") or []) if ch.get("verdict") == "WORSE"),
                "strips": strips,
            })
        return {"id": run_id, "name": run.name, "path": str(run), "cases": cases}

    def _rel(self, p: Path) -> str:
        """An id for a file under one of the roots: '<root index>/<relative path>'."""
        for i, root in enumerate(self.roots()):
            try:
                return f"{i}/{p.resolve().relative_to(root.resolve()).as_posix()}"
            except ValueError:
                continue
        raise ChecksError("file outside the check folders")

    def resolve(self, file_id: str) -> Path | None:
        """The path for an id from _rel, or None if it would leave the roots."""
        idx, _, rel = (file_id or "").partition("/")
        roots = self.roots()
        if not idx.isdigit() or int(idx) >= len(roots) or not rel:
            return None
        root = roots[int(idx)].resolve()
        p = (root / rel).resolve()
        try:
            p.relative_to(root)
        except ValueError:
            return None
        return p

    # ------------------------------------------------------------------ running

    def command(self, options: dict[str, Any]) -> tuple[str, list[str], dict[str, Any]]:
        if not self.tool.exists():
            raise ChecksError(f"{self.tool} is missing.")
        if not self.settings.get("python") and not self.settings.venv_python().exists():
            raise ChecksError("Checks need numpy and Pillow from the pipeline's Python environment. "
                              "Run Setup on the Library page first.")
        cases = Path(str(options.get("cases") or "").strip().strip('"'))
        if not cases.is_file():
            raise ChecksError("Pick a cases file (one 'rom | state | frames' per line).")
        if not parse_cases(cases.read_text(encoding="utf-8")):
            raise ChecksError(f"{cases.name} has no cases in it.")
        out_dir = self.settings.checks_out_dir()
        out_dir.mkdir(parents=True, exist_ok=True)
        args = [str(self.tool), "--cases", str(cases), "--out", str(out_dir)]
        serial = self.device.serial()
        args += ["--serial", serial]
        ir = str(options.get("ir") or "1").strip()
        if ir not in {"1", "2", "3", "4", "5", "6", "7", "8"}:
            raise ChecksError("Internal resolution must be 1 to 8.")
        args += ["--ir", ir]
        if options.get("packs"):
            args.append("--packs")
        renderers = str(options.get("renderers") or "vulkan,software").strip()
        if renderers not in ("vulkan,software", "vulkan,vulkan", "software,software"):
            raise ChecksError("Renderers must be vulkan,software or vulkan,vulkan.")
        args += ["--renderers", renderers]
        baseline = str(options.get("baseline") or "").strip()
        if baseline:
            b = self.resolve(baseline) if re.match(r"^\d+/", baseline) else Path(baseline.strip('"'))
            if b is None or not b.is_dir():
                raise ChecksError("The baseline folder doesn't exist. Pick an earlier run.")
            args += ["--baseline", str(b)]
        meta = {"cases_file": str(cases), "out_dir": str(out_dir), "baseline": baseline or None}
        return f"Checks: {cases.name}", [self.settings.python(), str(RUNNER)] + args, meta

    def snapshot_prefs(self, job: Job) -> None:
        """Before the run: remember the settings frame_compare changes, for a forced stop."""
        try:
            prefs = self.device.preferences()
        except DeviceError as e:
            job.append(f"studio: could not read the device settings beforehand ({e})")
            return
        job.meta["saved_prefs"] = {k: prefs[k] for k in CHANGED_PREFS if k in prefs}

    def restore_prefs(self, job: Job) -> None:
        """After a forced stop: frame_compare couldn't restore the settings itself, so we do."""
        saved = job.meta.get("saved_prefs") or {}
        if not saved:
            job.append("studio: no saved settings to restore; check the renderer and texture packs on the Thor")
            return
        for key, value in saved.items():
            try:
                if isinstance(value, bool):
                    self.device.set_preference(key, "true" if value else "false", "boolean")
                else:
                    self.device.set_preference(key, str(value), "string")
            except DeviceError as e:
                job.append(f"studio: could not restore {key}: {e}")
        try:
            self.device.close_emulator()
        except DeviceError:
            pass
        job.append("studio: restored " + ", ".join(saved) + " and closed the emulator")
