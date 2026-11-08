"""Run a backup job from start to finish."""

from __future__ import annotations

import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import traceback
from pathlib import Path

from . import __version__, config, destinations, identity, notify, paths, retention, snapshots, status, system
from .modules import registry
from .modules.base import Context
from .util import LifeboatError, Unavailable, file_lock, human_duration, human_size, log, require_root

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_SKIPPED = 75   # EX_TEMPFAIL; the systemd unit treats it as success
EXIT_CANCELLED = 130

PACMAN_DEBOUNCE = 10 * 60
RSH = str(Path(__file__).with_name("rsh-local"))
REMOTE = "lifeboat:"


def fake_super_opts() -> list[str]:
    """Run the 'remote' side of a local copy with --fake-super (rsync's -M doesn't work locally).

    Paths on that side must be written as remote(path).
    """
    return ["-s", "-e", RSH, "--rsync-path=rsync --fake-super"]


def remote(path: str) -> str:
    return REMOTE + path
PROGRESS_RE = re.compile(rb"^\s*([\d,]+)\s+(\d+)%\s+(\S+)\s+(\d+:\d\d:\d\d)")


class Cancelled(Exception):
    pass


class AlreadyRunning(Unavailable):
    pass


class TooSoon(Unavailable):
    """An event trigger fired again too soon after a backup. Not worth recording."""


def exit_code(result: dict) -> int:
    return {"success": EXIT_OK, "warning": EXIT_OK, "skipped": EXIT_SKIPPED,
            "cancelled": EXIT_CANCELLED}.get(result.get("status"), EXIT_FAILED)


# ---------------------------------------------------------------------------
# cancellation

class _CancelState:
    def __init__(self):
        self.cancelled = False
        self.proc: subprocess.Popen | None = None

    def handler(self, signum, frame):
        self.cancelled = True
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except OSError:
                pass

    def check(self):
        if self.cancelled:
            raise Cancelled()


_cancel = _CancelState()


def install_signal_handlers() -> None:
    _cancel.cancelled = False
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, _cancel.handler)


# ---------------------------------------------------------------------------
# rsync

def parse_stats(text: str) -> dict:
    def num(pattern):
        m = re.search(pattern, text)
        return int(m.group(1).replace(",", "")) if m else None
    return {
        "files": num(r"Number of files: ([\d,]+)"),
        "created": num(r"Number of created files: ([\d,]+)"),
        "deleted": num(r"Number of deleted files: ([\d,]+)"),
        "transferred_files": num(r"Number of regular files transferred: ([\d,]+)"),
        "total_size": num(r"Total file size: ([\d,]+) bytes"),
        "transferred_size": num(r"Total transferred file size: ([\d,]+) bytes"),
    }


def run_rsync(cmd: list[str], on_progress=None, out_log=None) -> tuple[int, str, list[str]]:
    """Run rsync, reporting progress. Returns (exit code, stdout text without progress, stderr lines)."""
    log.info("rsync: %s", " ".join(cmd))
    # The C locale keeps rsync's number formatting predictable for parsing.
    env = dict(os.environ, LC_ALL="C")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, env=env)
    _cancel.proc = proc
    errors: list[str] = []

    def read_err():
        for raw in proc.stderr:
            line = raw.decode(errors="replace").rstrip()
            if line:
                errors.append(line)
                log.warning("rsync: %s", line)

    t = threading.Thread(target=read_err, daemon=True)
    t.start()
    out_lines: list[str] = []
    buf = b""
    fd = proc.stdout.fileno()
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        buf += chunk
        parts = re.split(rb"[\r\n]", buf)
        buf = parts.pop()
        for part in parts:
            m = PROGRESS_RE.match(part)
            if m:
                if on_progress:
                    on_progress(int(m.group(1).replace(b",", b"")), int(m.group(2)),
                                m.group(3).decode(), m.group(4).decode())
            elif part.strip():
                out_lines.append(part.decode(errors="replace"))
    if buf.strip() and not PROGRESS_RE.match(buf):
        out_lines.append(buf.decode(errors="replace"))
    rc = proc.wait()
    t.join(timeout=5)
    _cancel.proc = None
    return rc, "\n".join(out_lines), errors


def _rsync_failure_message(rc: int, errors: list[str]) -> str:
    joined = "\n".join(errors)
    if "No space left on device" in joined:
        return "The destination is full. Free some space or keep fewer backups."
    if "Read-only file system" in joined:
        return "The destination became read-only during the backup."
    if "Stale file handle" in joined or "Input/output error" in joined:
        return "Lost contact with the destination during the backup."
    last = errors[-1] if errors else ""
    return f"rsync failed (exit {rc}). {last}".strip()


# ---------------------------------------------------------------------------
# hooks

def _run_hook(cmd: str, what: str, env_extra: dict) -> None:
    if not cmd.strip():
        return
    env = dict(os.environ, **env_extra)
    log.info("%s: %s", what, cmd)
    try:
        proc = subprocess.run(["/bin/sh", "-c", cmd], capture_output=True, text=True, env=env, timeout=3600)
    except subprocess.TimeoutExpired as exc:
        raise LifeboatError(f"{what} took longer than an hour and was stopped") from exc
    for line in (proc.stdout + proc.stderr).splitlines():
        log.info("%s: %s", what, line)
    if proc.returncode != 0:
        raise LifeboatError(f"{what} exited with code {proc.returncode}")


# ---------------------------------------------------------------------------
# pruning

def free_bytes(path: Path) -> int:
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def prune(jobdir: Path, job: config.Job, *, dry_run: bool = False, st: status.StatusWriter | None = None) -> dict:
    snaps = snapshots.list_snapshots(jobdir)
    dec = retention.decide(snaps, job.retention, time.time())
    removed = []
    for s in dec.remove:
        if st:
            st.update(phase=f"Removing old backup {s.name}")
        if not dry_run:
            log.info("pruning %s", s.name)
            snapshots.delete(jobdir, s.name)
        removed.append(s.name)
    if not dry_run:
        removed += space_guard(jobdir, job, st)
    return {"removed": removed, "keep": dec.keep}


def space_guard(jobdir: Path, job: config.Job, st=None) -> list[str]:
    r = job.retention
    if not r.min_free_gb or r.keep_all:
        return []
    need = r.min_free_gb * 1024 ** 3
    removed = []
    for victim in retention.space_victims(snapshots.list_snapshots(jobdir)):
        if free_bytes(jobdir) >= need:
            break
        if st:
            st.update(phase=f"Freeing space: removing {victim.name}")
        log.info("space guard: removing %s (free %s, want %s)", victim.name,
                 human_size(free_bytes(jobdir)), human_size(need))
        snapshots.delete(jobdir, victim.name)
        removed.append(victim.name)
    return removed


# ---------------------------------------------------------------------------
# logging

def _attach_log(job: str, started: float) -> tuple[logging.Handler | None, Path | None]:
    try:
        d = paths.LOG_DIR / job
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        p = d / (time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(started)) + ".log")
        h = logging.FileHandler(p, encoding="utf-8")
        os.chmod(p, 0o600)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
        h.setLevel(logging.INFO)
        log.addHandler(h)
        if log.level == logging.NOTSET or log.level > logging.INFO:
            log.setLevel(logging.INFO)
        return h, p
    except OSError:
        return None, None


def _rotate_logs(job: str, keep: int) -> None:
    d = paths.LOG_DIR / job
    try:
        logs = sorted(d.glob("*.log"))
    except OSError:
        return
    for old in logs[:-keep]:
        try:
            old.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# the run

def run_job(name: str, trigger: str = "manual", *, do_prune: bool = True) -> dict:
    require_root("Running a backup")
    cfg = config.load()
    job = cfg.job(name)
    started = time.time()
    result: dict = {"job": name, "action": "backup", "trigger": trigger, "started": started,
                    "status": "failed", "message": "", "snapshot": None, "warnings": [], "notes": [],
                    "stats": {}, "pruned": []}
    handler, log_path = _attach_log(name, started)
    result["log"] = log_path.name if log_path else None
    st: status.StatusWriter | None = None
    install_signal_handlers()
    record = True
    try:
        if not job.enabled and trigger != "manual":
            raise Unavailable("The job is turned off.")
        dest = cfg.destination_for(job)
        host = (cfg.settings.hostname or system.hostname()).lower()
        try:
            lock = file_lock(paths.LOCK_DIR / f"{name}.lock")
            lock.__enter__()
        except LifeboatError:
            record = False
            raise AlreadyRunning("This job is already running.") from None
        try:
            _check_event_trigger(job, trigger)
            st = status.StatusWriter(f"backup-{name}", name, "backup", trigger)
            log.info("Lifeboat %s: job '%s' (trigger: %s)", __version__, name, trigger)
            notify.ping(job.healthcheck_url, "start")
            st.update(phase="Running the before-backup command", force=True)
            _run_hook(job.pre_command, "Before-backup command", {"LIFEBOAT_JOB": name, "LIFEBOAT_TRIGGER": trigger})
            _cancel.check()
            st.update(phase="Connecting to the destination", force=True)
            with destinations.open_destination(dest, host) as opened:
                _backup(cfg, job, opened, trigger, started, st, result, do_prune)
        finally:
            lock.__exit__(None, None, None)
    except (AlreadyRunning, TooSoon) as exc:
        record = False
        result.update(status="skipped", message=str(exc))
    except Unavailable as exc:
        result.update(status="skipped", message=str(exc))
    except Cancelled:
        result.update(status="cancelled", message="Cancelled. The next run continues where this one stopped.")
    except LifeboatError as exc:
        result.update(status="failed", message=str(exc))
    except PermissionError as exc:
        log.error("permission error:\n%s", traceback.format_exc())
        result.update(status="failed", message=destinations.permission_message(exc.filename or "/")
                      if exc.filename else f"Permission denied: {exc}")
    except Exception as exc:  # unexpected: keep the traceback in the log
        log.error("unexpected error:\n%s", traceback.format_exc())
        result.update(status="failed", message=f"Unexpected error: {exc}")
    finally:
        if job.post_command.strip() and result["status"] != "skipped":
            try:
                _run_hook(job.post_command, "After-backup command",
                          {"LIFEBOAT_JOB": name, "LIFEBOAT_TRIGGER": trigger,
                           "LIFEBOAT_STATUS": result["status"], "LIFEBOAT_SNAPSHOT": result.get("snapshot") or ""})
            except LifeboatError as exc:
                result["warnings"].append(str(exc))
                if result["status"] == "success":
                    result["status"] = "warning"
        if st:
            st.close()
        result["finished"] = time.time()
        result["duration"] = result["finished"] - started
        if not result["message"]:
            result["message"] = _summary(result)
        log.info("result: %s: %s", result["status"], result["message"])
        for n in result["notes"]:
            log.info("note: %s", n)
        for w in result["warnings"]:
            log.warning("%s", w)
        if record:
            hist = {k: result[k] for k in ("job", "action", "trigger", "started", "finished", "duration",
                                           "status", "message", "snapshot", "log")}
            hist["stats"] = {k: v for k, v in result["stats"].items() if k in ("files", "total_size", "transferred_size")}
            hist["warning_count"] = len(result["warnings"])
            try:
                status.append_history(hist)
            except OSError as exc:
                log.warning("couldn't write history: %s", exc)
            notify.send(cfg, name, result)
            if result["status"] in ("success", "warning"):
                notify.ping(job.healthcheck_url, "", result["message"])
            elif result["status"] == "failed":
                notify.ping(job.healthcheck_url, "fail", result["message"])
        if handler:
            log.removeHandler(handler)
            handler.close()
        _rotate_logs(name, cfg.settings.log_keep)
    return result


def _summary(result: dict) -> str:
    s = result.get("stats") or {}
    if result["status"] in ("success", "warning") and s.get("total_size") is None:
        msg = f"Saved system info in {human_duration(result.get('duration'))}."
        return msg + (f" {len(result['warnings'])} warning(s)." if result["warnings"] else "")
    if result["status"] in ("success", "warning"):
        parts = [f"Backed up {human_size(s.get('total_size'))}"]
        if s.get("transferred_size") is not None:
            parts.append(f"{human_size(s.get('transferred_size'))} new")
        parts.append(f"in {human_duration(result.get('duration'))}")
        msg = ", ".join(parts) + "."
        if result["warnings"]:
            msg += f" {len(result['warnings'])} warning(s)."
        return msg
    return result["status"].capitalize()


def _check_event_trigger(job: config.Job, trigger: str) -> None:
    if trigger not in ("pacman", "drive"):
        return
    last = status.last_success(job.name)
    if not last:
        return
    age = time.time() - float(last.get("finished") or 0)
    if trigger == "pacman" and age < PACMAN_DEBOUNCE:
        raise TooSoon(f"Backed up {human_duration(age)} ago; skipping this pacman run.")
    hours = job.triggers.min_interval_hours
    if hours and age < hours * 3600:
        raise TooSoon(f"Backed up {human_duration(age)} ago (minimum gap: {hours}h).")


def _backup(cfg, job, opened, trigger, started, st, result, do_prune) -> None:
    cap = destinations.probe(opened)
    log.info("destination %s (%s), mode %s, free %s", opened.base, cap.fstype, cap.mode, human_size(cap.free_bytes))
    if cap.problems:
        raise LifeboatError(" ".join(cap.problems))
    if cap.mode == "lossy" and not job.allow_lossy:
        raise LifeboatError(cap.warnings[-1] + " To back up anyway (files restore owned by the user), "
                            "set allow_lossy = true on the job.")
    notes = [w for w in cap.warnings if not (cap.same_disk and opened.dest.same_disk_ok and "same disk" in w)]
    result["notes"] += notes

    identity.verify(opened.repo, opened.repo.name)
    identity.claim(opened.repo, opened.repo.name, [job.name])
    jobdir = snapshots.job_dir(opened.repo, job.name)
    snapshots.snaps_dir(jobdir).mkdir(parents=True, exist_ok=True)
    identity.save_job(jobdir, job)
    snapshots.cleanup(jobdir)
    pruned = space_guard(jobdir, job, st)

    st.update(phase="Planning", force=True)
    info = system.detect()
    users = system.users_for(job.users)
    ctx = Context(job=job, users=users, info=info,
                  allow_secrets=opened.encrypted or job.allow_unencrypted_secrets)
    plan, meta = registry.build(ctx, dest_paths=[str(opened.base), opened.mount_point])
    result["warnings"] += plan.warnings
    result["notes"] += plan.notes
    if not plan.paths and not meta:
        raise LifeboatError("Nothing to back up: none of the selected items exist on this system.")

    inprog = snapshots.in_progress(jobdir)
    resumed = inprog.is_dir()
    if resumed:
        log.info("resuming the unfinished run in %s", inprog)
    inprog.mkdir(exist_ok=True)
    files_dir = inprog / "files"
    files_dir.mkdir(exist_ok=True)
    meta_dir = inprog / "meta"
    shutil.rmtree(meta_dir, ignore_errors=True)
    meta_dir.mkdir()
    for mid, tasks in meta.items():
        for label, task in tasks:
            _cancel.check()
            st.update(phase=f"Saving {label}", force=True)
            try:
                result["warnings"] += task(meta_dir / mid)
            except Exception as exc:
                log.error("metadata task %s failed:\n%s", label, traceback.format_exc())
                result["warnings"].append(f"Couldn't save {label}: {exc}")

    _cancel.check()
    prev = snapshots.latest_good(jobdir)
    one_fs = job.one_file_system or "system" in job.modules
    with tempfile.TemporaryDirectory(prefix="lifeboat-") as tmp:
        lf = Path(tmp) / "files-from"
        ef = Path(tmp) / "excludes"
        lf.write_bytes(b"".join(p.lstrip("/").encode() + b"\0" for p in plan.paths))
        ef.write_bytes(b"".join(e.encode() + b"\0" for e in plan.excludes))
        fake = cap.mode == "fake-super"
        cmd = ["rsync", *cap.rsync_flags(), *(fake_super_opts() if fake else []),
               "-r", "--from0", f"--files-from={lf}", f"--exclude-from={ef}",
               "--delete", "--partial-dir=.rsync-partial", "--info=progress2", "--no-inc-recursive",
               "--stats"]
        if one_fs:
            cmd.append("-x")
        if prev:
            cmd.append(f"--link-dest={prev.files}")
        cmd += ["/", remote(f"{files_dir}/") if fake else f"{files_dir}/"]

        def progress(nbytes, pct, rate, eta):
            st.update(phase="Copying files", bytes=nbytes, percent=pct, rate=rate, eta=eta)

        st.update(phase="Scanning files", force=True)
        if plan.paths:
            rc, out, errors = run_rsync(cmd, progress)
        else:
            rc, out, errors = 0, "", []
    if _cancel.cancelled:
        raise Cancelled()

    stats = parse_stats(out)
    result["stats"] = stats
    status_word = "success"
    if rc == 24:
        log.info("some files vanished while copying (normal on a running system)")
    elif rc == 23:
        status_word = "warning"
        count = len(errors)
        result["warnings"].append(f"{count} file(s) couldn't be copied; see the log for details.")
    elif rc != 0:
        raise LifeboatError(_rsync_failure_message(rc, errors))
    if result["warnings"] and status_word == "success":
        status_word = "warning"

    st.update(phase="Finishing", percent=100, force=True)
    name = snapshots.new_name(jobdir, started)
    manifest = {
        "tool": f"lifeboat {__version__}", "job": job.name, "host": info.hostname, "trigger": trigger,
        "started": started, "finished": time.time(), "status": status_word, "resumed": resumed,
        "modules": job.modules, "users": [u.name for u in users], "paths": plan.paths,
        "excludes": plan.excludes, "metadata_mode": cap.mode, "rsync_flags": cap.rsync_flags(),
        "destination_fs": cap.fstype, "stats": stats, "warnings": result["warnings"][:200],
        "notes": result["notes"],
        "system": info.to_dict(), "previous": prev.name if prev else None,
    }
    snapshots.finalize(jobdir, name, manifest)
    result["snapshot"] = name
    result["status"] = status_word
    log.info("snapshot %s written", name)

    if do_prune and job.retention.prune_after_backup:
        st.update(phase="Removing old backups", force=True)
        pruned += prune(jobdir, job, st=st)["removed"]
    result["pruned"] = pruned
