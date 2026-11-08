"""The on-disk snapshot store.

<destination>/mikoshi/<host>/<job>/
    snapshots/
        2026-11-07_02-30-00/
            files/          normal copy of the backed-up paths, laid out from /
            meta/           module metadata (package lists, disk layout...)
            manifest.json   what was backed up, how, and the result
            pinned          present when the snapshot is kept forever
        .in-progress/       the run being written (renamed when it succeeds)
        .deleting-*/        snapshots being removed
    latest -> snapshots/<newest successful>
"""

from __future__ import annotations

import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from .util import MikoshiError, read_json, write_json

SNAP_DIR = "snapshots"
IN_PROGRESS = ".in-progress"
NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}(-\d{1,3})?$")
FORMAT = 1


@dataclass
class Snapshot:
    name: str
    path: Path
    time: float
    status: str = "unknown"
    pinned: bool = False
    note: str = ""
    manifest: dict = field(default_factory=dict)

    @property
    def files(self) -> Path:
        return self.path / "files"

    @property
    def meta(self) -> Path:
        return self.path / "meta"

    def to_dict(self) -> dict:
        m = self.manifest
        return {
            "name": self.name,
            "time": self.time,
            "status": self.status,
            "pinned": self.pinned,
            "note": self.note,
            "trigger": m.get("trigger", ""),
            "modules": m.get("modules", []),
            "paths": m.get("paths", []),
            "stats": m.get("stats", {}),
            "warnings": m.get("warnings", []),
            "metadata_mode": m.get("metadata_mode", ""),
            "duration": (m.get("finished", 0) - m.get("started", 0)) if m.get("finished") else None,
        }


def job_dir(repo: Path, job: str) -> Path:
    return repo / job


def snaps_dir(jobdir: Path) -> Path:
    return jobdir / SNAP_DIR


def check_name(name: str) -> str:
    if not NAME_RE.match(name):
        raise MikoshiError(f"'{name}' isn't a snapshot name (expected YYYY-MM-DD_HH-MM-SS)")
    return name


def _time_from_name(name: str) -> float:
    try:
        return time.mktime(time.strptime(name[:19], "%Y-%m-%d_%H-%M-%S"))
    except ValueError:
        return 0.0


def load(path: Path) -> Snapshot:
    manifest = read_json(path / "manifest.json", {}) or {}
    pin_file = path / "pinned"
    note = ""
    if pin_file.exists():
        try:
            note = pin_file.read_text(encoding="utf-8").strip()
        except OSError:
            pass
    return Snapshot(
        name=path.name,
        path=path,
        time=float(manifest.get("finished") or manifest.get("started") or _time_from_name(path.name)),
        status=manifest.get("status", "incomplete") if manifest else "incomplete",
        pinned=pin_file.exists(),
        note=note,
        manifest=manifest,
    )


def list_snapshots(jobdir: Path) -> list[Snapshot]:
    """All finished snapshots, newest first."""
    d = snaps_dir(jobdir)
    if not d.is_dir():
        return []
    out = []
    for entry in os.scandir(d):
        if entry.is_dir(follow_symlinks=False) and NAME_RE.match(entry.name):
            out.append(load(Path(entry.path)))
    out.sort(key=lambda s: (s.time, s.name), reverse=True)
    return out


def get(jobdir: Path, name: str) -> Snapshot:
    path = snaps_dir(jobdir) / check_name(name)
    if not path.is_dir():
        raise MikoshiError(f"No snapshot named {name}")
    return load(path)


def latest_good(jobdir: Path) -> Snapshot | None:
    for s in list_snapshots(jobdir):
        if s.status in ("success", "warning") and s.files.is_dir():
            return s
    return None


def new_name(jobdir: Path, t: float) -> str:
    base = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(t))
    name, n = base, 1
    while (snaps_dir(jobdir) / name).exists():
        n += 1
        name = f"{base}-{n}"
    return name


def in_progress(jobdir: Path) -> Path:
    return snaps_dir(jobdir) / IN_PROGRESS


def finalize(jobdir: Path, name: str, manifest: dict) -> Snapshot:
    """Write the manifest and turn the in-progress folder into a real snapshot."""
    src = in_progress(jobdir)
    manifest = dict(manifest, format=FORMAT)
    write_json(src / "manifest.json", manifest)
    dst = snaps_dir(jobdir) / name
    os.rename(src, dst)
    update_latest(jobdir, name)
    return load(dst)


def update_latest(jobdir: Path, name: str | None) -> None:
    link = jobdir / "latest"
    tmp = jobdir / ".latest.tmp"
    try:
        if tmp.is_symlink() or tmp.exists():
            tmp.unlink()
        if name is None:
            if link.is_symlink():
                link.unlink()
            return
        os.symlink(f"{SNAP_DIR}/{name}", tmp)
        os.replace(tmp, link)
    except OSError:
        pass  # a missing convenience link is not worth failing a backup over


def set_pinned(jobdir: Path, name: str, pinned: bool, note: str = "") -> None:
    snap = get(jobdir, name)
    pin = snap.path / "pinned"
    if pinned:
        pin.write_text(note.strip() + "\n" if note.strip() else "", encoding="utf-8")
    elif pin.exists():
        pin.unlink()


def delete(jobdir: Path, name: str) -> None:
    """Remove a snapshot. It's renamed first so a crash never leaves a half-deleted snapshot that looks valid."""
    snap = get(jobdir, name)
    trash = snaps_dir(jobdir) / f".deleting-{name}"
    os.rename(snap.path, trash)
    _rmtree(trash)
    good = latest_good(jobdir)
    update_latest(jobdir, good.name if good else None)


def cleanup(jobdir: Path) -> None:
    """Finish deletions interrupted by a crash."""
    d = snaps_dir(jobdir)
    if not d.is_dir():
        return
    for entry in os.scandir(d):
        if entry.name.startswith(".deleting-"):
            _rmtree(Path(entry.path))


def _rmtree(path: Path) -> None:
    def onexc(func, p, exc):
        # Snapshots keep original permissions (read-only folders); make writable and retry.
        try:
            os.chmod(os.path.dirname(p), 0o700)
            if not os.path.islink(p) and os.path.isdir(p):
                os.chmod(p, 0o700)
            func(p)
        except OSError:
            pass
    shutil.rmtree(path, onexc=onexc)
