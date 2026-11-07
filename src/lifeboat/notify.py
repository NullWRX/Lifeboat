"""Notifications: desktop pop-ups, ntfy, and Healthchecks-style pings."""

from __future__ import annotations

import os
import pwd
import time
import urllib.request

from . import APP_ID, APP_NAME, config, status
from .util import log, run, which

ICONS = {"success": "emblem-ok-symbolic", "warning": "dialog-warning-symbolic",
         "failed": "dialog-error-symbolic", "skipped": "dialog-information-symbolic",
         "cancelled": "process-stop-symbolic"}
NTFY_PRIORITY = {"success": "default", "warning": "default", "failed": "high", "skipped": "default",
                 "cancelled": "low"}
NTFY_TAGS = {"success": "white_check_mark", "warning": "warning", "failed": "rotating_light",
             "skipped": "zzz", "cancelled": "stop_sign"}
STALE_DAYS = 7


def should_notify(cfg: config.Config, job: str, result: dict) -> bool:
    mode = cfg.settings.notify_on
    st = result.get("status")
    if mode == "never" or st == "cancelled":
        return False
    if mode == "always":
        return True
    if st in ("failed", "warning"):
        return True
    if st == "skipped":
        # A drive that's rarely plugged in shouldn't nag every time, only when backups go stale.
        last = status.last_success(job)
        return last is None or time.time() - float(last.get("finished") or 0) > STALE_DAYS * 86400
    return False


def title_for(job: str, result: dict) -> str:
    st = result.get("status")
    return {
        "success": f"Backup '{job}' finished",
        "warning": f"Backup '{job}' finished with warnings",
        "failed": f"Backup '{job}' failed",
        "skipped": f"Backup '{job}' skipped",
        "cancelled": f"Backup '{job}' cancelled",
    }.get(st, f"Backup '{job}'")


def send(cfg: config.Config, job: str, result: dict) -> None:
    if not should_notify(cfg, job, result):
        return
    title = title_for(job, result)
    body = result.get("message") or ""
    if result.get("status") == "skipped":
        last = status.last_success(job)
        if last:
            days = int((time.time() - float(last.get("finished") or 0)) / 86400)
            body += f" Last successful backup: {days} days ago."
        else:
            body += " No successful backup yet."
    if cfg.settings.notify_desktop:
        desktop(title, body, result.get("status", ""))
    if cfg.settings.ntfy_url:
        ntfy(cfg, title, body, result.get("status", ""))


def _session_users() -> list[tuple[str, int]]:
    out = []
    try:
        entries = os.listdir("/run/user")
    except OSError:
        return out
    for e in entries:
        if not e.isdigit():
            continue
        uid = int(e)
        if uid < 1000 or not os.path.exists(f"/run/user/{uid}/bus"):
            continue
        try:
            out.append((pwd.getpwuid(uid).pw_name, uid))
        except KeyError:
            continue
    return out


def desktop(title: str, body: str, st: str) -> None:
    """Show a pop-up in every logged-in user's session."""
    for name, uid in _session_users():
        env = {"DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{uid}/bus", "PATH": "/usr/bin:/bin",
               "HOME": pwd.getpwuid(uid).pw_dir}
        icon = APP_ID
        if which("notify-send"):
            cmd = ["runuser", "-u", name, "--", "notify-send", "-a", APP_NAME, "-i", icon,
                   "-u", "critical" if st == "failed" else "normal", title, body]
        elif which("gdbus"):
            cmd = ["runuser", "-u", name, "--", "gdbus", "call", "--session",
                   "--dest", "org.freedesktop.Notifications",
                   "--object-path", "/org/freedesktop/Notifications",
                   "--method", "org.freedesktop.Notifications.Notify",
                   APP_NAME, "0", icon, title, body, "[]", "{}", "int32 8000"]
        else:
            return
        try:
            run(cmd, check=False, env=env, timeout=15)
        except Exception as exc:
            log.debug("desktop notification failed: %s", exc)


def ntfy(cfg: config.Config, title: str, body: str, st: str) -> None:
    headers = {"Title": title, "Priority": NTFY_PRIORITY.get(st, "default"),
               "Tags": NTFY_TAGS.get(st, "floppy_disk")}
    token = config.load_secrets().ntfy_token
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(cfg.settings.ntfy_url, data=(body or title).encode(), headers=headers, method="POST")
    try:
        urllib.request.urlopen(req, timeout=15).close()
    except Exception as exc:
        log.warning("ntfy notification failed: %s", exc)


def ping(url: str, suffix: str = "", body: str = "") -> None:
    """Healthchecks.io-style ping: url, url/start, url/fail."""
    if not url:
        return
    full = url.rstrip("/") + (f"/{suffix}" if suffix else "")
    req = urllib.request.Request(full, data=body.encode()[:10000] if body else None,
                                 method="POST" if body else "GET")
    try:
        urllib.request.urlopen(req, timeout=10).close()
    except Exception as exc:
        log.warning("health check ping failed: %s", exc)
