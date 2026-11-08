"""Carry over an install of Lifeboat, this project's earlier name.

Runs automatically after the package is installed (pacman install script), and
before any root command. Everything is moved, not copied, so it happens once:
config and secrets, run history, logs, the restore undo folder. Old timers and
the drive-plug rule are removed (they call a command that no longer exists)
and the new ones are written. Backup folders at destinations are renamed when
Mikoshi next opens them (see destinations.migrate_repo_dir).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import paths
from .util import log, run, which

LEGACY_NAME = "lifeboat"
LEGACY_CONFIG = Path("/etc/lifeboat")
LEGACY_STATE = Path("/var/lib/lifeboat")
LEGACY_LOGS = Path("/var/log/lifeboat")
LEGACY_UDEV = Path("/etc/udev/rules.d/90-lifeboat.rules")
LEGACY_MANAGED = "# Managed by Lifeboat."


def _legacy_timers() -> list[Path]:
    d = paths.SYSTEMD_DIR
    if not d.is_dir():
        return []
    out = []
    for f in d.glob(f"{LEGACY_NAME}-*.timer"):
        try:
            if f.read_text(encoding="utf-8").startswith(LEGACY_MANAGED):
                out.append(f)
        except OSError:
            continue
    return out


def needed() -> bool:
    return (LEGACY_CONFIG.is_dir() or LEGACY_STATE.is_dir() or LEGACY_LOGS.is_dir()
            or LEGACY_UDEV.exists() or bool(_legacy_timers()))


def _merge(src: Path, dst: Path) -> int:
    """Move src's entries into dst, merging folders and never overwriting files. Returns files moved."""
    dst.mkdir(parents=True, exist_ok=True)
    moved = 0
    for entry in os.scandir(src):
        target = dst / entry.name
        if target.exists():
            if entry.is_dir(follow_symlinks=False) and target.is_dir():
                moved += _merge(Path(entry.path), target)
            continue
        shutil.move(entry.path, target)
        moved += 1
    try:
        src.rmdir()
    except OSError:
        pass  # something was left because the new location already had a file of that name
    return moved


def _move_children(src: Path, dst: Path, msgs: list[str], what: str) -> None:
    if src.is_dir() and _merge(src, dst):
        msgs.append(f"Moved {what} from {src} to {dst}.")


def run_migration() -> list[str]:
    """Do the migration if there's anything to migrate. Safe to call any number of times."""
    if os.geteuid() != 0 or not needed():
        return []
    msgs: list[str] = []
    _move_children(LEGACY_CONFIG, paths.CONFIG_DIR, msgs, "the config")
    if paths.SECRETS_FILE.exists():
        os.chmod(paths.SECRETS_FILE, 0o600)
    if paths.CONFIG_FILE.exists():
        os.chmod(paths.CONFIG_FILE, 0o644)
    if LEGACY_STATE.is_dir():
        _move_children(LEGACY_STATE / "undo", paths.UNDO_DIR, msgs, "restore undo folders")
        _move_children(LEGACY_STATE, paths.STATE_DIR, msgs, "the run history")
    _move_children(LEGACY_LOGS, paths.LOG_DIR, msgs, "logs")

    live = os.path.isdir("/run/systemd/system") and which("systemctl")
    old_timers = _legacy_timers()
    for f in old_timers:
        if live:
            run(["systemctl", "disable", "--now", f.name], check=False, timeout=60)
        f.unlink(missing_ok=True)
    if old_timers:
        msgs.append(f"Removed {len(old_timers)} old Lifeboat timer(s).")
    if LEGACY_UDEV.exists():
        LEGACY_UDEV.unlink()
        msgs.append("Removed the old Lifeboat drive-plug rule.")

    # Write Mikoshi's own timers and rules for the migrated config.
    try:
        from . import config, schedule
        cfg = config.load(check=False)
        if live:
            run(["systemctl", "daemon-reload"], check=False, timeout=60)
        schedule.sync(cfg)
    except Exception as exc:  # the config is checked properly on the next command
        log.warning("couldn't set up schedules after migrating: %s", exc)
    if msgs:
        msgs.append("Backups at destinations move from 'lifeboat' to 'mikoshi' folders the next time "
                    "Mikoshi uses them.")
    for m in msgs:
        log.info("migrate: %s", m)
    return msgs
