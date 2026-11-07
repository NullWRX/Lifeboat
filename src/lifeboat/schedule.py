"""Turn job schedules and triggers into systemd timers and udev rules.

Lifeboat ships two template units (lifeboat@.service for scheduled and
manual runs, lifeboat-plug@.service for drive-plug runs). This module writes
one timer per scheduled job into /etc/systemd/system and keeps them in sync
with the config. Timers use Persistent=true, so a run missed while the PC
was off happens at the next boot.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from . import config, paths
from .util import LifeboatError, atomic_write, log, run, run_out, which

MANAGED = "# Managed by Lifeboat. Edit the job in Lifeboat instead; changes here are overwritten."


def systemd_running() -> bool:
    return os.path.isdir("/run/systemd/system")


def timer_unit(job: str) -> str:
    return f"lifeboat-{job}.timer"


def service_unit(job: str) -> str:
    return f"lifeboat@{job}.service"


def render_timer(job: config.Job) -> str:
    cal = job.schedule.on_calendar()
    lines = [MANAGED, "[Unit]", f"Description=Lifeboat backup schedule for job {job.name}", "",
             "[Timer]", f"OnCalendar={cal}", "Persistent=true", "AccuracySec=1min"]
    if job.schedule.random_delay_min:
        lines.append(f"RandomizedDelaySec={job.schedule.random_delay_min}min")
    lines += [f"Unit={service_unit(job.name)}", "", "[Install]", "WantedBy=timers.target", ""]
    return "\n".join(lines)


def check_calendar(expr: str) -> str:
    """Validate an OnCalendar expression. Returns the next elapse time as text."""
    if not which("systemd-analyze"):
        return ""
    proc = run(["systemd-analyze", "calendar", expr], check=False, timeout=15)
    if proc.returncode != 0:
        raise LifeboatError(f"Invalid schedule '{expr}': {proc.stderr.strip()}")
    m = re.search(r"Next elapse:\s*(.+)", proc.stdout)
    return m.group(1).strip() if m else ""


def _managed_timers() -> dict[str, Path]:
    out = {}
    d = paths.SYSTEMD_DIR
    if not d.is_dir():
        return out
    for f in d.glob("lifeboat-*.timer"):
        try:
            if f.read_text(encoding="utf-8").startswith(MANAGED):
                out[f.name] = f
        except OSError:
            continue
    return out


def _systemctl(*args: str) -> None:
    proc = run(["systemctl", *args], check=False, timeout=60)
    if proc.returncode != 0:
        log.warning("systemctl %s: %s", " ".join(args), proc.stderr.strip())


def sync(cfg: config.Config) -> list[str]:
    """Make the installed timers and udev rules match the config. Returns a list of changes."""
    changes: list[str] = []
    wanted: dict[str, str] = {}
    for job in cfg.jobs.values():
        if job.enabled and job.schedule.on_calendar():
            wanted[timer_unit(job.name)] = render_timer(job)
    existing = _managed_timers()
    live = systemd_running()
    to_enable, to_disable = [], []
    for unit, text in wanted.items():
        path = paths.SYSTEMD_DIR / unit
        old = path.read_text(encoding="utf-8") if path.exists() else None
        if old != text:
            atomic_write(path, text, mode=0o644)
            changes.append(f"{'updated' if old else 'added'} {unit}")
            to_enable.append(unit)
        elif live and run(["systemctl", "is-enabled", "--quiet", unit], check=False).returncode != 0:
            to_enable.append(unit)
    for unit, path in existing.items():
        if unit not in wanted:
            to_disable.append(unit)
            changes.append(f"removed {unit}")
    if live:
        for unit in to_disable:
            _systemctl("disable", "--now", unit)
        for unit in to_disable:
            existing[unit].unlink(missing_ok=True)
        _systemctl("daemon-reload")
        for unit in to_enable:
            _systemctl("enable", unit)
            _systemctl("restart", unit)
    else:
        for unit in to_disable:
            existing[unit].unlink(missing_ok=True)
        if wanted:
            changes.append("systemd isn't running here; timers were written but not started")
    changes += sync_udev(cfg)
    return changes


def render_udev(cfg: config.Config) -> str:
    rules = []
    for job in cfg.jobs.values():
        if not (job.enabled and job.triggers.on_drive_connect):
            continue
        dest = cfg.destinations.get(job.destination)
        if not dest or dest.type != "drive":
            continue
        rules.append(f'ACTION=="add", SUBSYSTEM=="block", ENV{{ID_FS_UUID}}=="{dest.uuid}", '
                     f'TAG+="systemd", ENV{{SYSTEMD_WANTS}}+="lifeboat-plug@{job.name}.service"')
    if not rules:
        return ""
    return MANAGED + "\n# Back up when a destination drive is plugged in.\n" + "\n".join(rules) + "\n"


def sync_udev(cfg: config.Config) -> list[str]:
    text = render_udev(cfg)
    path = paths.UDEV_RULES_FILE
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    if text == old:
        return []
    if text:
        atomic_write(path, text, mode=0o644)
    elif path.exists():
        path.unlink()
    if which("udevadm") and systemd_running():
        run(["udevadm", "control", "--reload"], check=False, timeout=30)
    return ["updated drive-plug rules" if text else "removed drive-plug rules"]


def cron_lines(cfg: config.Config) -> str:
    """Crontab entries equivalent to the schedules, for people who prefer cron."""
    out = ["# Lifeboat schedules as crontab entries (root's crontab). systemd timers are",
           "# the default; use these only if you disable the timers.", ""]
    for job in cfg.jobs.values():
        if not job.enabled:
            continue
        expr = job.schedule.cron()
        if expr:
            out.append(f"{expr} {paths.BIN} run --trigger timer {job.name}")
        elif job.schedule.mode == "custom":
            out.append(f"# {job.name}: custom systemd schedule '{job.schedule.calendar}' has no cron equivalent")
    return "\n".join(out) + "\n"


def next_run(job: str) -> float | None:
    """When the job's timer fires next (Unix time), or None."""
    if not systemd_running():
        return None
    out = run_out(["systemctl", "show", "--timestamp=unix", "-p", "NextElapseUSecRealtime", "--value",
                   timer_unit(job)], timeout=10).strip()
    if out.startswith("@"):
        try:
            return float(out[1:])
        except ValueError:
            return None
    return None
