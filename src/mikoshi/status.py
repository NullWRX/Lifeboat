"""Live status of running operations, and the history of past runs.

Status files (/run/mikoshi/status/*.json) and the history file
(/var/lib/mikoshi/history.jsonl) are world-readable and never contain file
paths, so the GUI can show progress without asking for a password.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import paths
from .util import read_json, write_json

HISTORY_MAX = 2000


class StatusWriter:
    def __init__(self, key: str, job: str, action: str, trigger: str = ""):
        self.path = paths.STATUS_DIR / f"{key}.json"
        self.data = {"key": key, "job": job, "action": action, "trigger": trigger, "state": "running",
                     "phase": "starting", "percent": None, "bytes": 0, "rate": "", "eta": "",
                     "started": time.time(), "pid": os.getpid(), "message": ""}
        self._last = 0.0
        self.write(force=True)

    def update(self, force: bool = False, **fields) -> None:
        self.data.update(fields)
        self.write(force)

    def write(self, force: bool = False) -> None:
        t = time.time()
        if not force and t - self._last < 0.5:
            return
        self._last = t
        try:
            write_json(self.path, self.data, mode=0o644)
        except OSError:
            pass

    def close(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def running() -> list[dict]:
    out = []
    d = paths.STATUS_DIR
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.json")):
        data = read_json(f)
        if not data:
            continue
        if not _alive(int(data.get("pid") or 0)):
            continue  # stale file from a crashed run
        out.append(data)
    return out


def running_for(job: str) -> dict | None:
    for s in running():
        if s.get("job") == job:
            return s
    return None


def append_history(entry: dict) -> None:
    f = paths.HISTORY_FILE
    f.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, sort_keys=True) + "\n"
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(line)
    os.chmod(f, 0o644)
    _trim(f)


def _trim(f: Path) -> None:
    try:
        if f.stat().st_size < 1_000_000:
            return
        lines = f.read_text(encoding="utf-8").splitlines(keepends=True)
        if len(lines) > HISTORY_MAX:
            tmp = f.with_suffix(".tmp")
            tmp.write_text("".join(lines[-HISTORY_MAX:]), encoding="utf-8")
            os.chmod(tmp, 0o644)
            os.replace(tmp, f)
    except OSError:
        pass


def history(job: str | None = None, limit: int = 100, action: str | None = None) -> list[dict]:
    """Newest first."""
    try:
        lines = paths.HISTORY_FILE.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, PermissionError):
        return []
    out = []
    for line in reversed(lines):
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if job and e.get("job") != job:
            continue
        if action and e.get("action") != action:
            continue
        out.append(e)
        if len(out) >= limit:
            break
    return out


def last_run(job: str, action: str = "backup") -> dict | None:
    h = history(job, limit=1, action=action)
    return h[0] if h else None


def last_success(job: str) -> dict | None:
    for e in history(job, limit=500, action="backup"):
        if e.get("status") in ("success", "warning"):
            return e
    return None
