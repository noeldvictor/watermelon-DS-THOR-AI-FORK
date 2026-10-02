"""Background jobs: a subprocess per job, its output kept in memory and streamed to the browser.

Jobs run in lanes. A lane runs one job at a time and queues the rest, so two GPU upscales or two
device runs never overlap ("pipeline" for the remaster steps and setup, "device" for anything that
drives the Thor). History lives in memory only and is gone when the studio stops.
"""
from __future__ import annotations

import collections
import itertools
import os
import signal
import subprocess
import threading
import time
from typing import Any, Callable

IS_WINDOWS = os.name == "nt"
MAX_LINES = 20000          # per job; older lines are dropped (the index keeps counting)
FINAL = ("done", "failed", "cancelled")

Hook = Callable[["Job"], None]


class Job:
    """One command and its output. Line indexes are absolute, so a reader can resume."""

    def __init__(self, job_id: str, title: str, cmd: list[str], *, kind: str, lane: str,
                 game: str | None = None, cwd: str | None = None, env: dict[str, str] | None = None,
                 meta: dict[str, Any] | None = None, grace: float = 10.0,
                 prepare: Hook | None = None, after_kill: Hook | None = None):
        self.id = job_id
        self.title = title
        self.cmd = cmd
        self.kind = kind
        self.lane = lane
        self.game = game
        self.cwd = cwd
        self.env = env or {}
        self.meta = meta or {}
        self.grace = grace              # seconds a cancelled job gets to stop by itself
        self.prepare = prepare          # runs right before the process starts
        self.after_kill = after_kill    # runs when the process had to be killed
        self.status = "queued"
        self.returncode: int | None = None
        self.created = time.time()
        self.started: float | None = None
        self.ended: float | None = None
        self.proc: subprocess.Popen | None = None
        self.cancel_requested = False
        self.killed = False
        self._lines: list[str] = []
        self._base = 0
        self._cond = threading.Condition()

    # ------------------------------------------------------------------ output

    def append(self, text: str) -> None:
        with self._cond:
            self._lines.append(text)
            overflow = len(self._lines) - MAX_LINES
            if overflow > 0:
                del self._lines[:overflow]
                self._base += overflow
            self._cond.notify_all()

    def set_status(self, status: str, returncode: int | None = None) -> None:
        with self._cond:
            self.status = status
            if returncode is not None:
                self.returncode = returncode
            if status == "running":
                self.started = time.time()
            if status in FINAL:
                self.ended = time.time()
            self._cond.notify_all()

    @property
    def finished(self) -> bool:
        return self.status in FINAL

    @property
    def line_count(self) -> int:
        with self._cond:
            return self._base + len(self._lines)

    def read(self, start: int, wait: float = 0.0) -> tuple[int, list[str], str]:
        """Lines from absolute index `start` on, waiting up to `wait` s when there are none yet.

        Returns (index of the first line returned, lines, status). Lines dropped by the cap are
        skipped, so the first index can be later than `start`.
        """
        with self._cond:
            if wait > 0 and start >= self._base + len(self._lines) and not self.finished:
                self._cond.wait(wait)
            start = max(start, self._base)
            return start, self._lines[start - self._base:], self.status

    def tail(self, count: int) -> list[str]:
        with self._cond:
            return self._lines[-count:] if count > 0 else []

    def summary(self) -> dict[str, Any]:
        end = self.ended or time.time()
        return {
            "id": self.id, "title": self.title, "kind": self.kind, "lane": self.lane, "game": self.game,
            "status": self.status, "returncode": self.returncode, "created": self.created,
            "started": self.started, "ended": self.ended,
            "duration": (end - self.started) if self.started else None,
            "lines": self.line_count, "command": subprocess.list2cmdline(self.cmd), "meta": self.meta,
        }


class JobManager:
    """Queues jobs per lane and runs them on one worker thread per lane."""

    def __init__(self, popen: Callable[..., subprocess.Popen] = subprocess.Popen, default_grace: float = 10.0):
        self._popen = popen
        self._default_grace = default_grace
        self._lock = threading.Condition()
        self._jobs: dict[str, Job] = {}
        self._queues: dict[str, collections.deque[Job]] = {}
        self._running: dict[str, Job | None] = {}
        self._workers: dict[str, threading.Thread] = {}
        self._ids = itertools.count(1)

    def submit(self, title: str, cmd: list[str], *, kind: str, lane: str = "pipeline",
               **options: Any) -> Job:
        options.setdefault("grace", self._default_grace)
        job = Job(f"j{next(self._ids)}", title, cmd, kind=kind, lane=lane, **options)
        with self._lock:
            self._jobs[job.id] = job
            self._queues.setdefault(lane, collections.deque()).append(job)
            ahead = self._running.get(lane)
            if ahead is not None or len(self._queues[lane]) > 1:
                job.append(f"studio: queued behind '{(ahead or self._queues[lane][0]).title}'")
            if lane not in self._workers:
                worker = threading.Thread(target=self._worker, args=(lane,), name=f"jobs-{lane}", daemon=True)
                self._workers[lane] = worker
                worker.start()
            self._lock.notify_all()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self, game: str | None = None, kind: str | None = None) -> list[Job]:
        with self._lock:
            jobs = list(self._jobs.values())
        jobs = [j for j in jobs if (game is None or j.game == game) and (kind is None or j.kind == kind)]
        return sorted(jobs, key=lambda j: j.created, reverse=True)

    def cancel(self, job_id: str) -> Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.finished:
                return job
            queue = self._queues.get(job.lane)
            if job.status == "queued" and queue is not None and job in queue:
                queue.remove(job)
                job.append("studio: removed from the queue")
                job.set_status("cancelled")
                return job
            job.cancel_requested = True
        if job.proc is not None:
            threading.Thread(target=self._stop, args=(job,), daemon=True).start()
        return job

    def shutdown(self, max_grace: float = 15.0) -> None:
        """Stop whatever still runs (the studio is closing): politely first, then by force."""
        with self._lock:
            for queue in self._queues.values():
                for job in queue:
                    job.set_status("cancelled")
                queue.clear()
        running = [j for j in self.list() if j.status == "running" and j.proc is not None]
        for job in running:
            job.cancel_requested = True
            job.grace = min(job.grace, max_grace)
            self._stop(job)
        deadline = time.time() + 15
        for job in running:   # let the workers run clean-up hooks (frame_compare's restore)
            while not job.finished and time.time() < deadline:
                time.sleep(0.1)

    # ------------------------------------------------------------------ internals

    def _worker(self, lane: str) -> None:
        while True:
            with self._lock:
                while not self._queues[lane]:
                    self._lock.wait()
                job = self._queues[lane].popleft()
                self._running[lane] = job
            try:
                self._run(job)
            except Exception as e:  # never let a worker die
                job.append(f"studio: internal error: {type(e).__name__}: {e}")
                job.set_status("failed")
            finally:
                with self._lock:
                    self._running[lane] = None

    def _run(self, job: Job) -> None:
        if job.cancel_requested:
            job.set_status("cancelled")
            return
        job.set_status("running")
        if job.prepare is not None:
            try:
                job.prepare(job)
            except Exception as e:
                job.append(f"studio: preparation step failed: {e}")
        env = dict(os.environ)
        env.update({"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"})
        env.update(job.env)
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if IS_WINDOWS else 0
        try:
            proc = self._popen(job.cmd, cwd=job.cwd, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               creationflags=flags, start_new_session=not IS_WINDOWS)
        except OSError as e:
            job.append(f"studio: could not start {job.cmd[0]}: {e}")
            job.set_status("failed")
            return
        job.proc = proc
        if job.cancel_requested:   # cancelled while starting
            threading.Thread(target=self._stop, args=(job,), daemon=True).start()
        assert proc.stdout is not None
        for raw in iter(proc.stdout.readline, b""):
            text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if "\r" in text:            # progress bars redraw a line with \r: keep the last state
                text = text.rsplit("\r", 1)[-1]
            job.append(text)
        rc = proc.wait()
        if job.killed and job.after_kill is not None:
            try:
                job.after_kill(job)
            except Exception as e:
                job.append(f"studio: clean-up after stopping failed: {e}")
        if job.cancel_requested:
            job.set_status("cancelled", rc)
        else:
            job.set_status("done" if rc == 0 else "failed", rc)

    def _stop(self, job: Job) -> None:
        proc = job.proc
        if proc is None or proc.poll() is not None:
            return
        job.append("studio: stopping...")
        try:
            if IS_WINDOWS:
                os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(proc.pid, signal.SIGINT)
        except (OSError, ValueError):
            pass
        try:
            proc.wait(timeout=job.grace)
            return
        except subprocess.TimeoutExpired:
            pass
        job.killed = True
        job.append(f"studio: it did not stop within {job.grace:.0f} s; killing it")
        _kill_tree(proc)


def _kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=30)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    if proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass
