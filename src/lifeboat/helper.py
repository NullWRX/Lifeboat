"""Privileged helper for the GUI.

The GUI runs as the logged-in user and starts this helper once with
`pkexec lifeboat helper` (one password prompt per session). They talk in
JSON lines over stdin/stdout:

    -> {"id": 1, "method": "list_snapshots", "params": {"job": "daily"}}
    <- {"id": 1, "result": {...}}       or   {"id": 1, "error": "message"}

Only the methods below exist; every argument is validated the same way the
CLI validates it. Long work (backups, restores, pruning) is handed to systemd
so it keeps running if the GUI closes.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
import traceback

from . import __version__, cli, config, destinations, engine, launch, paths, restore, schedule, snapshots, system
from .util import LifeboatError, file_lock, log

IDLE_CLOSE = 90  # seconds a mounted destination stays open between requests


class Helper:
    def __init__(self):
        self.open_jobs: dict[str, tuple[contextlib.ExitStack, object, object, float]] = {}

    # -- destination cache -------------------------------------------------
    def _job(self, name: str):
        self._expire()
        if name in self.open_jobs:
            stack, job, jobdir, _ = self.open_jobs[name]
            self.open_jobs[name] = (stack, job, jobdir, time.time())
            return job, jobdir
        cfg = config.load()
        stack = contextlib.ExitStack()
        job, opened, jobdir = stack.enter_context(restore.open_job(cfg, name))
        self.open_jobs[name] = (stack, job, jobdir, time.time())
        return job, jobdir

    def _expire(self, everything: bool = False) -> None:
        for name, (stack, _, _, used) in list(self.open_jobs.items()):
            if everything or time.time() - used > IDLE_CLOSE:
                with contextlib.suppress(Exception):
                    stack.close()
                del self.open_jobs[name]

    def _forget(self, name: str) -> None:
        if name in self.open_jobs:
            with contextlib.suppress(Exception):
                self.open_jobs[name][0].close()
            del self.open_jobs[name]

    # -- methods ------------------------------------------------------------
    def ping(self):
        return {"version": __version__, "uid": os.geteuid()}

    def get_config(self):
        cfg = config.load(check=False)
        return {"config": config.to_dict(cfg), "has_ntfy_token": bool(config.load_secrets().ntfy_token)}

    def validate_config(self, config_dict):
        cfg, errors = config.from_dict(config_dict)
        return {"errors": errors or config.validate(cfg)}

    def save_config(self, config_dict, ntfy_token=None):
        cfg, errors = config.from_dict(config_dict)
        errors = errors or config.validate(cfg)
        if errors:
            raise config.ConfigError(errors)
        for job in cfg.jobs.values():
            if job.schedule.mode == "custom":
                schedule.check_calendar(job.schedule.calendar)
        if ntfy_token is not None:
            sec = config.load_secrets()
            sec.ntfy_token = ntfy_token
            config.save_secrets(sec)
        self._expire(everything=True)
        return {"changes": cli.apply_config(cfg)}

    def check_calendar(self, expr):
        return {"next": schedule.check_calendar(expr)}

    def check_destination(self, destination, name="test"):
        cfg, errors = config.from_dict({"destinations": {name: destination}})
        errors = errors or [e for e in config.validate(cfg, module_ids=set()) if e.startswith("destinations")]
        if errors:
            raise config.ConfigError(errors)
        main = config.load(check=False)
        host = (main.settings.hostname or system.hostname()).lower()
        return cli.check_destination(cfg.destinations[name], host)

    def drives(self):
        return destinations.list_drives()

    def exports(self, server):
        return destinations.nfs_exports(server)

    def list_snapshots(self, job):
        _, jobdir = self._job(job)
        return {"job": job, "location": str(jobdir),
                "snapshots": [s.to_dict() for s in snapshots.list_snapshots(jobdir)]}

    def browse(self, job, snapshot, path="/", area="files"):
        _, jobdir = self._job(job)
        if area not in ("files", "meta"):
            raise LifeboatError("bad area")
        return restore.browse(snapshots.get(jobdir, snapshot), path, area)

    def read_meta(self, job, snapshot, path):
        _, jobdir = self._job(job)
        return restore.read_meta(snapshots.get(jobdir, snapshot), path)

    def restore_preview(self, job, snapshot, items, target=None, delete=False, force=False):
        _, jobdir = self._job(job)
        return restore.restore(snapshots.get(jobdir, snapshot), items, target=target or None,
                               delete=bool(delete), dry_run=True, force=bool(force))

    def start_restore(self, job, snapshot, items, target=None, delete=False, force=False):
        _, jobdir = self._job(job)
        snapshots.get(jobdir, snapshot)
        return {"key": launch.start_restore(job, snapshot, list(items), target or None, bool(delete), bool(force))}

    def start_backup(self, job):
        config.load().job(job)
        self._forget(job)
        return {"unit": launch.start_backup(job)}

    def cancel(self, key):
        return {"cancelled": launch.cancel(key)}

    def prune_preview(self, job):
        j, jobdir = self._job(job)
        res = engine.prune(jobdir, j, dry_run=True)
        return res

    def start_prune(self, job):
        config.load().job(job)
        self._forget(job)
        return {"key": launch.start_prune(job)}

    def pin(self, job, snapshot, pinned=True, note=""):
        _, jobdir = self._job(job)
        snapshots.set_pinned(jobdir, snapshot, bool(pinned), str(note))
        return {"ok": True}

    def delete_snapshot(self, job, snapshot):
        _, jobdir = self._job(job)
        with file_lock(paths.LOCK_DIR / f"{job}.lock"):
            snapshots.delete(jobdir, snapshot)
        return {"ok": True}

    def list_logs(self, job):
        config.load().job(job)
        d = paths.LOG_DIR / job
        return sorted((f.name for f in d.glob("*.log")), reverse=True) if d.is_dir() else []

    def read_log(self, job, name=None):
        config.load().job(job)
        d = paths.LOG_DIR / job
        logs = sorted(d.glob("*.log")) if d.is_dir() else []
        if not logs:
            return ""
        f = d / name if name else logs[-1]
        if f.parent != d or not f.is_file():
            raise LifeboatError("No such log")
        data = f.read_bytes()
        return data[-1_000_000:].decode(errors="replace")


METHODS = {name for name in dir(Helper) if not name.startswith("_")}


def serve() -> int:
    if os.geteuid() != 0:
        print(json.dumps({"id": 0, "error": "The helper must run as root (pkexec lifeboat helper)."}), flush=True)
        return 1
    # stdout carries the protocol; logging goes to stderr only.
    h = Helper()
    out = sys.stdout
    print(json.dumps({"id": 0, "result": {"ready": True, "version": __version__}}), file=out, flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = None
        try:
            req = json.loads(line)
            rid = req.get("id")
            method = req.get("method")
            params = req.get("params") or {}
            if method not in METHODS or not isinstance(params, dict):
                raise LifeboatError(f"Unknown request: {method}")
            result = getattr(h, method)(**params)
            resp = {"id": rid, "result": result}
        except (LifeboatError, engine.Unavailable) as exc:
            resp = {"id": req.get("id") if isinstance(req, dict) else None, "error": str(exc),
                    "unavailable": isinstance(exc, engine.Unavailable)}
        except TypeError as exc:
            resp = {"id": req.get("id") if isinstance(req, dict) else None, "error": f"Bad request: {exc}"}
        except Exception as exc:
            log.error("helper error:\n%s", traceback.format_exc())
            resp = {"id": req.get("id") if isinstance(req, dict) else None, "error": f"Unexpected error: {exc}"}
        print(json.dumps(resp, default=str), file=out, flush=True)
    h._expire(everything=True)
    return 0
