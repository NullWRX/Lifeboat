"""Tidy up backup locations that the config no longer uses.

When a job moves to another destination, a job is deleted, or a destination is
deleted or pointed somewhere else, the old place still holds Lifeboat's folder.
It's owned by root (backups hold every user's files), so users can't remove it.

Rules:
  * Only this PC's folder (<base>/lifeboat/<host>) is ever touched; other PCs
    sharing the destination are left alone.
  * Folders without backups (no snapshots, no unfinished run) are removed
    automatically.
  * Folders with backups are only removed when the user explicitly says so.
"""

from __future__ import annotations

import contextlib
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import config, destinations, paths, snapshots
from .util import LifeboatError, Unavailable, file_lock, log

README = "README.txt"


def location_key(d: config.Destination) -> tuple:
    """Identifies the physical place a destination points to."""
    sub = os.path.normpath(d.subdir or ".")
    if d.type == "folder":
        return ("folder", os.path.normpath(d.path))
    if d.type == "drive":
        return ("drive", d.uuid.lower(), sub)
    return ("nfs", d.server.lower(), os.path.normpath(d.export), sub)


@dataclass
class Stale:
    destination: config.Destination
    jobs: list[str] | None          # job folders to clean; None = every job folder of this PC

    def to_dict(self) -> dict:
        return {"destination": asdict(self.destination), "jobs": self.jobs}


def find_stale(old: config.Config, new: config.Config) -> list[Stale]:
    """Locations (and job folders in them) that the old config used and the new one doesn't."""
    new_keys = {location_key(d) for d in new.destinations.values()}
    new_users: dict[tuple, set[str]] = {}
    for job in new.jobs.values():
        d = new.destinations.get(job.destination)
        if d:
            new_users.setdefault(location_key(d), set()).add(job.name)
    out: dict[tuple, Stale] = {}
    for name, d in old.destinations.items():
        key = location_key(d)
        if key not in new_keys:
            out[key] = Stale(d, None)
            continue
        before = {j.name for j in old.jobs.values() if j.destination == name}
        orphaned = before - new_users.get(key, set())
        if orphaned:
            prev = out.get(key)
            jobs = sorted(orphaned | set(prev.jobs or [])) if prev else sorted(orphaned)
            out[key] = Stale(d, jobs)
    return list(out.values())


def _job_dirs(host_dir: Path, jobs: list[str] | None) -> list[Path]:
    if not host_dir.is_dir():
        return []
    out = []
    for e in sorted(os.scandir(host_dir), key=lambda e: e.name):
        if e.is_dir(follow_symlinks=False) and (jobs is None or e.name in jobs):
            out.append(Path(e.path))
    return out


def _describe_job(jd: Path) -> dict:
    snaps = snapshots.list_snapshots(jd)
    partial = snapshots.in_progress(jd).is_dir()
    return {"job": jd.name, "snapshots": len(snaps), "partial": partial,
            "newest": snaps[0].time if snaps else None, "has_data": bool(snaps) or partial}


@dataclass
class Report:
    label: str
    location: str = ""
    available: bool = True
    message: str = ""
    jobs: list[dict] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    destination: dict = field(default_factory=dict)   # the old destination, for a follow-up delete
    scope: list[str] | None = None                    # job folders in question (None = all of this PC's)

    @property
    def has_data(self) -> bool:
        return any(j["has_data"] for j in self.jobs)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["has_data"] = self.has_data
        return d


def tidy(base: Path, host: str) -> list[str]:
    """Remove this PC's folder if it's empty, and the lifeboat folder if nothing else is in it."""
    removed = []
    top = base / paths.REPO_DIRNAME
    host_dir = top / host
    with contextlib.suppress(OSError):
        if host_dir.is_dir() and not any(host_dir.iterdir()):
            host_dir.rmdir()
            removed.append(str(host_dir))
    with contextlib.suppress(OSError):
        if top.is_dir() and {p.name for p in top.iterdir()} <= {README}:
            (top / README).unlink(missing_ok=True)
            top.rmdir()
            removed.append(str(top))
    return removed


def _clean(base: Path, host: str, jobs: list[str] | None, delete_backups: bool, report: Report) -> None:
    host_dir = base / paths.REPO_DIRNAME / host
    for jd in _job_dirs(host_dir, jobs):
        info = _describe_job(jd)
        if info["has_data"] and not delete_backups:
            report.kept.append(jd.name)
            continue
        try:
            with file_lock(paths.LOCK_DIR / f"{jd.name}.lock"):
                log.info("removing %s", jd)
                snapshots._rmtree(jd)
            report.removed.append(jd.name)
        except LifeboatError:
            report.kept.append(jd.name)
            report.message = f"Job '{jd.name}' is running, so its folder was left for now."
    tidy(base, host)


def process(dest: config.Destination, host: str, jobs: list[str] | None, *,
            delete_backups: bool = False) -> Report:
    """Inspect a stale location; remove what's empty (or everything, if asked)."""
    report = Report(label=f"{dest.name} ({dest.describe()})", destination=asdict(dest), scope=jobs)
    try:
        with destinations.open_destination(dest, host, create=False) as opened:
            host_dir = opened.base / paths.REPO_DIRNAME / host
            report.location = str(host_dir)
            report.jobs = [_describe_job(jd) for jd in _job_dirs(host_dir, jobs)]
            _clean(opened.base, host, jobs, delete_backups, report)
            report.jobs = [j for j in report.jobs if j["job"] not in report.removed]
    except Unavailable as exc:
        report.available = False
        report.message = str(exc)
    except LifeboatError as exc:
        report.available = False
        report.message = str(exc)
    return report


def after_save(old: config.Config, new: config.Config, host: str) -> list[Report]:
    """Run after the config changed: clean empty leftovers, report locations that still hold backups."""
    reports = []
    for stale in find_stale(old, new):
        r = process(stale.destination, host, stale.jobs)
        if r.removed or r.has_data or not r.available:
            reports.append(r)
    return reports


def clean_path(path: str, host: str, *, delete_backups: bool, protect: set[str], dry_run: bool = False) -> Report:
    """CLI: clean this PC's Lifeboat folder at a path (a folder holding 'lifeboat', or that folder).

    Job folders in `protect` (jobs that still back up here) are never touched. With dry_run,
    only report what's there.
    """
    base = Path(path)
    if base.name == paths.REPO_DIRNAME:
        base = base.parent
    if not (base / paths.REPO_DIRNAME).is_dir():
        raise LifeboatError(f"No Lifeboat folder in {path}")
    report = Report(label=str(base), location=str(base / paths.REPO_DIRNAME / host))
    host_dir = base / paths.REPO_DIRNAME / host
    jobs = [jd.name for jd in _job_dirs(host_dir, None) if jd.name not in protect]
    report.jobs = [_describe_job(host_dir / j) for j in jobs]
    report.kept += sorted(protect & {jd.name for jd in _job_dirs(host_dir, None)})
    if dry_run:
        return report
    _clean(base, host, jobs, delete_backups, report)
    report.jobs = [j for j in report.jobs if j["job"] not in report.removed]
    return report


def summary(r: Report) -> str:
    return summary_jobs(r.jobs)


def summary_jobs(jobs: list[dict]) -> str:
    parts = []
    for j in jobs:
        if j["snapshots"]:
            newest = time.strftime("%Y-%m-%d", time.localtime(j["newest"]))
            parts.append(f"'{j['job']}': {j['snapshots']} backup{'s' if j['snapshots'] != 1 else ''} (newest {newest})")
        elif j["partial"]:
            parts.append(f"'{j['job']}': an unfinished backup")
    return "; ".join(parts)
