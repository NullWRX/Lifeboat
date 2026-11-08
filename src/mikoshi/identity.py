"""Which computer a backup folder belongs to, and finding existing backups.

Each PC's folder at a destination (<base>/mikoshi/<name>/) holds host.json:

    {"name": "nspc", "machines": ["3f9c..."], "hostname": "nspc", "updated": ..., "jobs": [...]}

"machines" holds one-way fingerprints of /etc/machine-id (the ID itself is
never stored; systemd treats it as confidential). A reinstalled PC gets a new
machine-id, so adopting a folder adds the new fingerprint instead of replacing
the old one: restoring /etc later brings the old ID back, and it still matches.

Each job folder holds job.json: the job's settings at its last backup, so the
job can be recreated on a fresh install.
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

from . import __version__, config, paths, snapshots, system
from .util import MikoshiError, read_json, write_json

HOST_FILE = "host.json"
JOB_FILE = "job.json"


def machine_tag() -> str:
    # MIKOSHI_MACHINE_ID exists for tests only; pkexec and sudo clear the environment.
    mid = os.environ.get("MIKOSHI_MACHINE_ID", "")
    if not mid:
        try:
            mid = Path("/etc/machine-id").read_text().strip()
        except OSError:
            mid = ""
    if not mid:
        return ""
    return hashlib.sha256(f"mikoshi:{mid}".encode()).hexdigest()[:24]


def legacy_tag() -> str:
    """The fingerprint Lifeboat (this project's earlier name) wrote for this machine."""
    mid = os.environ.get("MIKOSHI_MACHINE_ID", "")
    if not mid:
        try:
            mid = Path("/etc/machine-id").read_text().strip()
        except OSError:
            mid = ""
    return hashlib.sha256(f"lifeboat:{mid}".encode()).hexdigest()[:24] if mid else ""


def is_this_machine(machines: list[str]) -> bool:
    tags = {t for t in (machine_tag(), legacy_tag()) if t}
    return bool(tags & set(machines))


def read_host(host_dir: Path) -> dict:
    return read_json(host_dir / HOST_FILE, {}) or {}


class OtherComputer(MikoshiError):
    """The folder for this PC's name belongs to a different computer."""


def verify(host_dir: Path, name: str) -> None:
    """Refuse to write into a folder another computer owns."""
    info = read_host(host_dir)
    machines = info.get("machines") or []
    tag = machine_tag()
    if machines and tag and not is_this_machine(machines):
        raise OtherComputer(
            f"Another computer already saves its backups as '{name}' at this destination. "
            "Give this PC a different name (Destinations tab, This PC), or if this is the same PC "
            "after a reinstall, use Find Backups and choose Use These Backups.")


def claim(host_dir: Path, name: str, jobs: list[str] | None = None) -> None:
    """Record that this computer writes here (adds its fingerprint; never removes others)."""
    host_dir.mkdir(parents=True, exist_ok=True)
    info = read_host(host_dir)
    machines = list(info.get("machines") or [])
    tag = machine_tag()
    if tag and tag not in machines:
        machines.append(tag)
    known_jobs = sorted(set(info.get("jobs") or []) | set(jobs or []))
    write_json(host_dir / HOST_FILE, {
        "name": name, "machines": machines, "hostname": system.hostname(), "updated": time.time(),
        "mikoshi": __version__, "jobs": known_jobs, "format": 1,
    })


def save_job(jobdir: Path, job: config.Job) -> None:
    entry = config.to_dict(config.Config(jobs={job.name: job}))["jobs"][job.name]
    write_json(jobdir / JOB_FILE, {"name": job.name, "job": entry, "saved": time.time(), "mikoshi": __version__})


# ---------------------------------------------------------------------------
# discovery

def _job_from_manifest(name: str, manifest: dict) -> dict:
    """Best guess at a job's settings for folders made before job.json existed."""
    job = config.to_dict(config.Config(jobs={name: config.Job(name=name)}))["jobs"][name]
    job["modules"] = [m for m in manifest.get("modules", []) if m]
    return job


def describe_host(host_dir: Path) -> dict:
    info = read_host(host_dir)
    jobs = []
    newest_any = None
    for e in sorted(os.scandir(host_dir), key=lambda e: e.name):
        if not e.is_dir(follow_symlinks=False) or not config.NAME_RE.match(e.name):
            continue
        jd = Path(e.path)
        snaps = snapshots.list_snapshots(jd)
        if not snaps and not snapshots.in_progress(jd).is_dir():
            continue
        saved = read_json(jd / JOB_FILE, {}) or {}
        job_cfg = saved.get("job")
        guessed = False
        if not isinstance(job_cfg, dict):
            job_cfg = _job_from_manifest(e.name, snaps[0].manifest if snaps else {})
            guessed = True
        newest = snaps[0].time if snaps else None
        if newest and (newest_any is None or newest > newest_any):
            newest_any = newest
        jobs.append({"name": e.name, "snapshots": len(snaps), "newest": newest,
                     "modules": job_cfg.get("modules", []), "config": job_cfg, "guessed": guessed})
    machines = info.get("machines") or []
    return {"name": host_dir.name, "hostname": info.get("hostname", ""), "newest": newest_any,
            "jobs": jobs, "this_machine": is_this_machine(machines), "marked": bool(info)}


def discover(base: Path) -> list[dict]:
    """Every PC folder with backups under <base>/mikoshi."""
    top = base / paths.REPO_DIRNAME
    if not top.is_dir():
        return []
    out = []
    for e in sorted(os.scandir(top), key=lambda e: e.name):
        if e.is_dir(follow_symlinks=False) and config.NAME_RE.match(e.name):
            d = describe_host(Path(e.path))
            if d["jobs"]:
                out.append(d)
    return out


def imported_job(entry: dict, name: str, destination: str) -> tuple[config.Job, list[str]]:
    """A job recreated from a backup folder. Returns the job and notes for the user.

    For safety it starts turned off (a fresh system's backups mustn't push out the old ones
    before they've been restored), and commands that run as root are not carried over: a
    folder on a shared NAS could have been written by someone else.
    """
    data = dict(entry)
    notes = []
    for key in ("pre_command", "post_command"):
        if data.get(key):
            notes.append(f"'{name}': its {key.replace('_', ' ')} wasn't imported ({data[key]!r}). Re-add it if you need it.")
            data[key] = ""
    data["destination"] = destination
    data["enabled"] = False
    cfg, errors = config.from_dict({"jobs": {name: data}})
    if errors:
        raise MikoshiError(f"Job '{name}' in the backup has settings this version doesn't understand: {errors[0]}")
    return cfg.jobs[name], notes
