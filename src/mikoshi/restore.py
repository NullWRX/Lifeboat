"""Browse snapshots and restore files from them."""

from __future__ import annotations

import contextlib
import os
import re
import stat
import time
import traceback
from pathlib import Path
from typing import Iterator

from . import config, destinations, engine, paths, snapshots, status, system
from .snapshots import Snapshot
from .util import MikoshiError, is_within, log, require_root

# Restoring over these on a running system can break it; needs --force.
CRITICAL = ["/usr", "/bin", "/sbin", "/lib", "/lib64", "/boot", "/efi"]
MAX_CHANGES = 5000


@contextlib.contextmanager
def open_job(cfg: config.Config, name: str) -> Iterator[tuple[config.Job, destinations.Opened, Path]]:
    job = cfg.job(name)
    dest = cfg.destination_for(job)
    host = (cfg.settings.hostname or system.hostname()).lower()
    with destinations.open_destination(dest, host, create=False) as opened:
        yield job, opened, snapshots.job_dir(opened.repo, name)


def _norm(p: str) -> str:
    if not p.startswith("/"):
        raise MikoshiError(f"'{p}' must be an absolute path")
    n = os.path.normpath(p)
    if ".." in n.split("/"):
        raise MikoshiError(f"'{p}' can't contain '..'")
    return n


def _inside(snap: Snapshot, area: str, p: str) -> Path:
    base = snap.files if area == "files" else snap.meta
    target = base / p.lstrip("/")
    if p == "/":
        return base
    real_base = os.path.realpath(base)
    # Every component must be a real directory inside the snapshot, never a symlink out of it.
    if not is_within(os.path.realpath(target.parent), real_base):
        raise MikoshiError("Path leaves the snapshot")
    return target


def browse(snap: Snapshot, path: str = "/", area: str = "files") -> list[dict]:
    p = _norm(path)
    target = _inside(snap, area, p)
    if target.is_symlink() or not target.is_dir():
        raise MikoshiError(f"{path} isn't a folder in this backup")
    out = []
    with os.scandir(target) as it:
        for e in it:
            try:
                s = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISDIR(s.st_mode):
                kind = "dir"
            elif stat.S_ISLNK(s.st_mode):
                kind = "link"
            elif stat.S_ISREG(s.st_mode):
                kind = "file"
            else:
                kind = "other"
            item = {"name": e.name, "path": os.path.join(p, e.name), "kind": kind,
                    "size": s.st_size if kind == "file" else None, "mtime": s.st_mtime}
            if kind == "link":
                try:
                    item["target"] = os.readlink(e.path)
                except OSError:
                    item["target"] = "?"
            out.append(item)
    out.sort(key=lambda i: (i["kind"] != "dir", i["name"].lower()))
    return out


def read_meta(snap: Snapshot, rel: str, limit: int = 2_000_000) -> str:
    target = _inside(snap, "meta", _norm("/" + rel.lstrip("/")))
    if target.is_symlink() or not target.is_file():
        raise MikoshiError(f"{rel} isn't a file in this backup's metadata")
    with open(target, "rb") as fh:
        return fh.read(limit).decode(errors="replace")


def restore_flags(snap: Snapshot) -> list[str]:
    mode = snap.manifest.get("metadata_mode", "native")
    used = snap.manifest.get("rsync_flags", [])
    if mode == "fake-super":
        # Owners and permissions are read back out of the user.rsync.* xattrs by the sending
        # side, which runs with --fake-super (see engine.fake_super_opts).
        return ["-aHAX", "--numeric-ids", *engine.fake_super_opts()]
    if mode == "lossy":
        return ["-rlptH"]
    flags = ["-aH", "--numeric-ids"]
    if "-A" in used:
        flags.append("-A")
    if "-X" in used:
        flags.append("-X")
    return flags


def _check_original_target(p: str, force: bool) -> None:
    for c in CRITICAL:
        if p == "/" or is_within(p, c):
            if not force:
                raise MikoshiError(f"Restoring {p} over the running system can break it. "
                                    "Restore to a different folder, or use --force if you're sure.")
    # Refuse if any existing parent is a symlink: a user could point it at /etc.
    parent = os.path.dirname(p) or "/"
    probe = parent
    while not os.path.lexists(probe) and probe != "/":
        probe = os.path.dirname(probe)
    if os.path.realpath(probe) != os.path.normpath(probe):
        raise MikoshiError(f"{probe} is a symbolic link; restore to a different folder instead.")


UNDO_KEEP_DAYS = 30


def _clean_undo() -> None:
    """Drop undo folders from restores more than a month old."""
    if not paths.UNDO_DIR.is_dir():
        return
    cutoff = time.time() - UNDO_KEEP_DAYS * 86400
    for entry in os.scandir(paths.UNDO_DIR):
        try:
            if entry.is_dir(follow_symlinks=False) and entry.stat(follow_symlinks=False).st_mtime < cutoff:
                snapshots._rmtree(Path(entry.path))
        except OSError:
            continue


def _owner_of(path: str) -> tuple[int, int]:
    probe = path
    while not os.path.exists(probe) and probe != "/":
        probe = os.path.dirname(probe)
    s = os.stat(probe)
    return s.st_uid, s.st_gid


_ITEM_RE = re.compile(r"^(\S{11}|\*deleting)\s+(.*)$")


def _classify(lines: list[str]) -> dict:
    counts = {"new": 0, "changed": 0, "deleted": 0, "attributes": 0}
    items = []
    for line in lines:
        m = _ITEM_RE.match(line.strip("\n"))
        if not m:
            continue
        code, name = m.group(1), m.group(2)
        if code == "*deleting":
            kind = "deleted"
        elif "+++++++" in code:
            kind = "new"
        elif code[0] in "<>ch":
            kind = "changed"      # content (or link target) differs
        else:
            kind = "attributes"   # only permissions, owner or time differ
        if kind == "attributes" and code[1] == "d":
            continue  # folder timestamp noise
        counts[kind] += 1
        if len(items) < MAX_CHANGES:
            items.append({"change": kind, "path": "/" + name.lstrip("/")})
    return {"counts": counts, "items": items, "truncated": sum(counts.values()) > len(items)}


def restore(snap: Snapshot, items: list[str], *, target: str | None = None, delete: bool = False,
            dry_run: bool = False, force: bool = False, st: status.StatusWriter | None = None) -> dict:
    require_root("Restoring")
    if not items:
        raise MikoshiError("Choose at least one file or folder to restore")
    norm = [_norm(p) for p in items]
    for p in norm:
        src = _inside(snap, "files", p)
        if not os.path.lexists(src):
            raise MikoshiError(f"{p} isn't in this backup")
    flags = restore_flags(snap)
    lossy = snap.manifest.get("metadata_mode") == "lossy"
    undo = None
    if target is None:
        for p in norm:
            _check_original_target(p, force)
        root = "/"
        if not dry_run:
            _clean_undo()
            undo = paths.UNDO_DIR / time.strftime("%Y-%m-%d_%H-%M-%S")
            flags += ["--backup", f"--backup-dir={undo}"]
    else:
        root = _norm(target)
        if is_within(os.path.realpath(root), os.path.realpath(snap.path)):
            raise MikoshiError("Can't restore into the backup itself")
        if not dry_run:
            os.makedirs(root, exist_ok=True)
    if delete:
        flags.append("--delete")
    groups = [[p] for p in norm] if lossy else [norm]
    results = {"dry_run": dry_run, "target": root, "undo": str(undo) if undo else None, "items": norm}
    all_lines: list[str] = []
    stats_total: dict = {}
    for group in groups:
        cmd = ["rsync", *flags, "-R"]
        if lossy:
            uid, gid = _owner_of(os.path.join(root, group[0].lstrip("/")))
            cmd.append(f"--chown={uid}:{gid}")
        if dry_run:
            cmd += ["-n", "--itemize-changes", "--out-format=%i %n%L"]
        else:
            cmd += ["--info=progress2", "--no-inc-recursive", "--stats"]
        fake = snap.manifest.get("metadata_mode") == "fake-super"
        cmd += [(engine.remote if fake else str)(f"{snap.files}/./{p.lstrip('/')}") for p in group]
        cmd.append(root.rstrip("/") + "/")

        def progress(nbytes, pct, rate, eta):
            if st:
                st.update(phase="Restoring files", bytes=nbytes, percent=pct, rate=rate, eta=eta)

        rc, out, errors = engine.run_rsync(cmd, progress)
        if rc not in (0, 23, 24):
            raise MikoshiError(engine._rsync_failure_message(rc, errors))
        if rc == 23:
            results.setdefault("errors", []).extend(errors[-50:])
        if dry_run:
            all_lines += out.splitlines()
        else:
            for k, v in engine.parse_stats(out).items():
                if v is not None:
                    stats_total[k] = stats_total.get(k, 0) + v
    if dry_run:
        results["changes"] = _classify(all_lines)
    else:
        results["stats"] = stats_total
    return results


def run_restore(job_name: str, snap_name: str, items: list[str], *, target: str | None, delete: bool,
                force: bool, key: str | None = None) -> dict:
    """A restore as a tracked operation (status file + history), used by the CLI and the GUI."""
    require_root("Restoring")
    cfg = config.load()
    key = key or f"restore-{int(time.time())}"
    st = status.StatusWriter(key, job_name, "restore")
    engine.install_signal_handlers()
    started = time.time()
    entry = {"job": job_name, "action": "restore", "trigger": "manual", "started": started,
             "snapshot": snap_name, "status": "failed", "message": ""}
    result: dict = {}
    try:
        st.update(phase="Connecting to the destination", force=True)
        with open_job(cfg, job_name) as (job, opened, jobdir):
            snap = snapshots.get(jobdir, snap_name)
            st.update(phase="Restoring files", force=True)
            result = restore(snap, items, target=target, delete=delete, force=force, st=st)
        entry["status"] = "success" if not result.get("errors") else "warning"
        where = target or "their original location"
        entry["message"] = f"Restored {len(items)} item(s) from {snap_name} to {where}."
        if result.get("undo"):
            entry["message"] += f" Replaced files were saved in {result['undo']}."
    except engine.Cancelled:
        entry.update(status="cancelled", message="Restore cancelled; some files may already be restored.")
    except MikoshiError as exc:
        entry["message"] = str(exc)
    except Exception as exc:
        log.error("restore failed:\n%s", traceback.format_exc())
        entry["message"] = f"Unexpected error: {exc}"
    finally:
        st.close()
        entry["finished"] = time.time()
        entry["duration"] = entry["finished"] - started
        try:
            status.append_history(entry)
        except OSError:
            pass
    result.update(entry)
    return result
