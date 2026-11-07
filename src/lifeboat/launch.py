"""Start long operations in the background so they outlive the GUI or the SSH session."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import time

from . import paths, schedule, status
from .util import LifeboatError, run


def _detached(args: list[str]) -> None:
    subprocess.Popen([paths.BIN, *args], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)


def start_backup(job: str) -> str:
    if status.running_for(job):
        raise LifeboatError(f"Job '{job}' is already running")
    # A transient unit (not the timer's lifeboat@.service) so the run is recorded as manual.
    return _transient(f"run-{job}", f"Lifeboat backup (job {job})", ["run", job])


def _transient(name: str, description: str, args: list[str]) -> str:
    if schedule.systemd_running():
        unit = f"lifeboat-{name}"
        run(["systemd-run", f"--unit={unit}", f"--description={description}", "--collect",
             "--property=Nice=10", "--property=IOSchedulingClass=idle", paths.BIN, *args], timeout=30)
        return unit
    _detached(args)
    return "background process"


def new_key(prefix: str) -> str:
    return f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid() % 10000}"


def start_restore(job: str, snap: str, items: list[str], target: str | None, delete: bool, force: bool) -> str:
    key = new_key("restore")
    opts = ["--key", key, "--yes"]
    if target:
        opts += ["--to", target]
    if delete:
        opts.append("--delete")
    if force:
        opts.append("--force")
    _transient(key, f"Lifeboat restore from {job}", ["restore", *opts, job, snap, "--", *items])
    return key


def start_prune(job: str) -> str:
    key = new_key("prune")
    _transient(key, f"Lifeboat prune of {job}", ["prune", "--key", key, job])
    return key


def cancel(key_or_job: str) -> bool:
    """Ask a running operation to stop (it cleans up and records the cancel)."""
    if not re.match(r"^[a-z0-9_-]+$", key_or_job):
        raise LifeboatError("bad operation name")
    for s in status.running():
        if s.get("key") == key_or_job or s.get("job") == key_or_job:
            try:
                os.kill(int(s["pid"]), signal.SIGTERM)
                return True
            except ProcessLookupError:
                return False
    return False
