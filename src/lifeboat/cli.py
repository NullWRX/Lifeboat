"""Command-line interface. Everything the GUI does is available here, for SSH and scripts."""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import (APP_NAME, __version__, config, destinations, engine, launch, overview, paths, restore,
               schedule, snapshots, status, system)
from .modules import registry
from .util import LifeboatError, file_lock, human_duration, human_size, log, require_root

STATUS_ICON = {"success": "ok", "warning": "warning", "failed": "FAILED", "skipped": "skipped",
               "cancelled": "cancelled"}


def out_json(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def fmt_time(t) -> str:
    if not t:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(t)))


def ago(t) -> str:
    if not t:
        return "never"
    d = time.time() - float(t)
    if d < 90:
        return "just now"
    if d < 3600:
        return f"{int(d // 60)} min ago"
    if d < 86400 * 2:
        return f"{int(d // 3600)} h ago"
    return f"{int(d // 86400)} days ago"


def confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        raise LifeboatError(f"{question} Re-run with --yes to confirm.")
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def table(rows: list[list[str]], headers: list[str]) -> None:
    widths = [max(len(str(r[i])) for r in rows + [headers]) for i in range(len(headers))]
    print("  ".join(h.ljust(w) for h, w in zip(headers, widths)).rstrip())
    for r in rows:
        print("  ".join(str(c).ljust(w) for c, w in zip(r, widths)).rstrip())


def _progress_printer(key: str, stop: threading.Event) -> None:
    """Show live progress from the status file on a single terminal line."""
    last = ""
    path = paths.STATUS_DIR / f"{key}.json"
    while not stop.wait(0.5):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        pct = data.get("percent")
        line = data.get("phase", "")
        if pct is not None and data.get("phase") in ("Copying files", "Restoring files"):
            line += f"  {pct:3d}%  {human_size(data.get('bytes'))}  {data.get('rate', '')}  ETA {data.get('eta', '')}"
        if line != last:
            cols = shutil.get_terminal_size((80, 20)).columns
            sys.stderr.write("\r" + line[: cols - 1].ljust(min(len(last), cols - 1)))
            sys.stderr.flush()
            last = line
    if last:
        sys.stderr.write("\r" + " " * min(len(last), shutil.get_terminal_size((80, 20)).columns - 1) + "\r")
        sys.stderr.flush()


# ---------------------------------------------------------------------------
# commands

def cmd_status(a) -> int:
    cfg = config.load()
    jobs = overview.jobs(cfg)
    if a.json:
        out_json({"jobs": jobs, "running": status.running()})
        return 0
    if not jobs:
        print("No jobs yet. Create one with `sudo lifeboat config edit` or the Lifeboat app.")
        return 0
    rows = []
    for j in jobs:
        last = j["last_run"]
        last_txt = f"{STATUS_ICON.get(last['status'], last['status'])} {ago(last.get('finished'))}" if last else "never"
        if j["running"]:
            pct = j["running"].get("percent")
            last_txt = "running" + (f" {pct}%" if pct is not None else "")
        rows.append([j["name"], j["destination"], j["schedule"], last_txt,
                     fmt_time(j["next_run"]) if j["next_run"] else "-"])
    table(rows, ["JOB", "DESTINATION", "SCHEDULE", "LAST RUN", "NEXT RUN"])
    return 0


def cmd_run(a) -> int:
    require_root("Running a backup")
    stop = threading.Event()
    t = None
    if sys.stderr.isatty() and not a.verbose:
        t = threading.Thread(target=_progress_printer, args=(f"backup-{a.job}", stop), daemon=True)
        t.start()
    try:
        result = engine.run_job(a.job, a.trigger, do_prune=not a.no_prune)
    finally:
        stop.set()
        if t:
            t.join(2)
    if a.json:
        out_json(result)
    else:
        print(f"{STATUS_ICON.get(result['status'], result['status'])}: {result['message']}")
        for w in result["warnings"]:
            print(f"  warning: {w}")
        if result.get("pruned"):
            print(f"  removed old backups: {', '.join(result['pruned'])}")
    return engine.exit_code(result)


def cmd_start(a) -> int:
    require_root("Starting a backup")
    where = launch.start_backup(a.job)
    print(f"Started '{a.job}' ({where}). Follow it with `lifeboat status`.")
    return 0


def cmd_cancel(a) -> int:
    require_root("Cancelling")
    if launch.cancel(a.what):
        print("Asked it to stop.")
        return 0
    print("Nothing running by that name.")
    return 1


def _open(job: str):
    return restore.open_job(config.load(), job)


def cmd_list(a) -> int:
    require_root("Listing backups")
    with _open(a.job) as (job, opened, jobdir):
        snaps = snapshots.list_snapshots(jobdir)
        if a.json:
            out_json({"job": a.job, "location": str(jobdir), "snapshots": [s.to_dict() for s in snaps]})
            return 0
        if not snaps:
            print(f"No backups yet for '{a.job}'.")
            return 0
        rows = []
        for s in snaps:
            st = s.manifest.get("stats", {})
            rows.append([s.name, STATUS_ICON.get(s.status, s.status), s.manifest.get("trigger", ""),
                         human_size(st.get("total_size")), human_size(st.get("transferred_size")),
                         "pinned" + (f": {s.note}" if s.note else "") if s.pinned else ""])
        table(rows, ["SNAPSHOT", "RESULT", "TRIGGER", "SIZE", "NEW DATA", ""])
        print(f"\nStored in {jobdir}")
    return 0


def cmd_show(a) -> int:
    require_root("Reading backups")
    with _open(a.job) as (job, opened, jobdir):
        s = snapshots.get(jobdir, a.snapshot)
        if a.json:
            out_json(dict(s.to_dict(), manifest=s.manifest))
            return 0
        m = s.manifest
        print(f"Snapshot  {s.name}  ({STATUS_ICON.get(s.status, s.status)})")
        print(f"Location  {s.path}")
        print(f"Trigger   {m.get('trigger', '-')}    Duration {human_duration((m.get('finished') or 0) - (m.get('started') or 0))}")
        print(f"Modules   {', '.join(m.get('modules', []))}")
        st = m.get("stats", {})
        print(f"Size      {human_size(st.get('total_size'))} in {st.get('files', '?')} items, "
              f"{human_size(st.get('transferred_size'))} new")
        print(f"Storage   {m.get('metadata_mode', '?')} on {m.get('destination_fs', '?')}")
        print("Paths:")
        for p in m.get("paths", []):
            print(f"  {p}")
        for w in m.get("warnings", []):
            print(f"warning: {w}")
    return 0


def cmd_browse(a) -> int:
    require_root("Reading backups")
    with _open(a.job) as (job, opened, jobdir):
        s = snapshots.get(jobdir, a.snapshot)
        items = restore.browse(s, a.path, "meta" if a.meta else "files")
        if a.json:
            out_json(items)
            return 0
        for i in items:
            extra = f" -> {i['target']}" if i["kind"] == "link" else ""
            size = human_size(i["size"]) if i["size"] is not None else ""
            print(f"{'d' if i['kind'] == 'dir' else '-'} {size:>10}  {fmt_time(i['mtime'])}  {i['name']}{'/' if i['kind'] == 'dir' else ''}{extra}")
    return 0


def _print_changes(res: dict) -> None:
    c = res["changes"]["counts"]
    print(f"{c['new']} new, {c['changed']} changed, {c['attributes']} with different permissions/times, "
          f"{c['deleted']} to delete")
    for item in res["changes"]["items"][:40]:
        print(f"  {item['change']:<10} {item['path']}")
    total = sum(c.values())
    if total > 40:
        print(f"  ... and {total - 40} more")


def cmd_restore(a) -> int:
    require_root("Restoring")
    if a.key:
        res = restore.run_restore(a.job, a.snapshot, a.paths, target=a.to, delete=a.delete, force=a.force, key=a.key)
        if a.json:
            out_json(res)
        else:
            print(res.get("message", ""))
        return 0 if res.get("status") in ("success", "warning") else 1
    with _open(a.job) as (job, opened, jobdir):
        s = snapshots.get(jobdir, a.snapshot)
        preview = restore.restore(s, a.paths, target=a.to, delete=a.delete, dry_run=True, force=a.force)
        if a.dry_run:
            out_json(preview) if a.json else _print_changes(preview)
            return 0
        if not a.json:
            print(f"Restoring from {s.name} to {a.to or 'the original location'}:")
            _print_changes(preview)
        if not confirm("Go ahead?", a.yes):
            print("Nothing changed.")
            return 1
    res = restore.run_restore(a.job, a.snapshot, a.paths, target=a.to, delete=a.delete, force=a.force)
    out_json(res) if a.json else print(res.get("message", ""))
    return 0 if res.get("status") in ("success", "warning") else 1


def cmd_prune(a) -> int:
    require_root("Pruning")
    cfg = config.load()
    st = status.StatusWriter(a.key, a.job, "prune") if a.key else None
    try:
        with contextlib.ExitStack() as stack:
            if not a.dry_run:
                stack.enter_context(file_lock(paths.LOCK_DIR / f"{a.job}.lock"))
            job, opened, jobdir = stack.enter_context(restore.open_job(cfg, a.job))
            res = engine.prune(jobdir, job, dry_run=a.dry_run, st=st)
    finally:
        if st:
            st.close()
    if a.json:
        out_json(res)
        return 0
    if not res["removed"]:
        print("Nothing to remove.")
    else:
        verb = "Would remove" if a.dry_run else "Removed"
        print(f"{verb}: {', '.join(res['removed'])}")
    if a.dry_run:
        for name, why in sorted(res["keep"].items(), reverse=True):
            print(f"  keep {name}  ({', '.join(why)})")
    return 0


def cmd_pin(a, pinned: bool) -> int:
    require_root("Pinning")
    with _open(a.job) as (job, opened, jobdir):
        snapshots.set_pinned(jobdir, a.snapshot, pinned, getattr(a, "note", "") or "")
    print(("Pinned " if pinned else "Unpinned ") + a.snapshot)
    return 0


def cmd_delete(a) -> int:
    require_root("Deleting backups")
    with _open(a.job) as (job, opened, jobdir):
        snapshots.get(jobdir, a.snapshot)
        if not confirm(f"Delete backup {a.snapshot} of '{a.job}' for good?", a.yes):
            return 1
        with file_lock(paths.LOCK_DIR / f"{a.job}.lock"):
            snapshots.delete(jobdir, a.snapshot)
    print(f"Deleted {a.snapshot}")
    return 0


def check_destination(dest: config.Destination, hostname: str) -> dict:
    res = {"ok": False, "available": True, "message": "", "capabilities": None, "location": ""}
    try:
        with destinations.open_destination(dest, hostname) as opened:
            cap = destinations.probe(opened)
            res["capabilities"] = cap.to_dict()
            res["location"] = str(opened.repo)
            res["ok"] = not cap.problems
            res["message"] = " ".join(cap.problems) or "Ready for backups."
    except engine.Unavailable as exc:
        res.update(available=False, message=str(exc))
    except LifeboatError as exc:
        res["message"] = str(exc)
    return res


def cmd_check(a) -> int:
    require_root("Testing a destination")
    cfg = config.load()
    if a.destination not in cfg.destinations:
        raise LifeboatError(f"No destination named '{a.destination}'")
    res = check_destination(cfg.destinations[a.destination], (cfg.settings.hostname or system.hostname()).lower())
    if a.json:
        out_json(res)
        return 0 if res["ok"] else 1
    print(res["message"])
    cap = res["capabilities"]
    if cap:
        print(f"  filesystem   {cap['fstype']}   free {human_size(cap['free_bytes'])} of {human_size(cap['total_bytes'])}")
        print(f"  storage mode {cap['mode']}   encrypted: {'yes' if cap['encrypted'] else 'no'}")
        for w in cap["warnings"]:
            print(f"  note: {w}")
    return 0 if res["ok"] else 1


def cmd_drives(a) -> int:
    drives = destinations.list_drives()
    if a.json:
        out_json(drives)
        return 0
    rows = [[d["uuid"], d["fstype"], human_size(d["size"]), d["label"], d["model"],
             ", ".join(d["mountpoints"]) or "-", "system" if d["system"] else ("no (FAT/exFAT)" if not d["usable"] else "")]
            for d in drives]
    table(rows, ["UUID", "TYPE", "SIZE", "LABEL", "MODEL", "MOUNTED AT", "NOTE"])
    return 0


def cmd_exports(a) -> int:
    for e in destinations.nfs_exports(a.server):
        print(e)
    return 0


def cmd_modules(a) -> int:
    mods = registry.describe()
    if a.json:
        out_json({"modules": mods, "presets": registry.PRESETS})
        return 0
    for m in mods:
        tags = [t for t, on in (("needs encrypted destination", m["sensitive"]), ("large", m["heavy"])) if on]
        print(f"{m['id']:<12} {m['name']}" + (f"  [{', '.join(tags)}]" if tags else ""))
        print(f"{'':<12} {m['summary']}")
    return 0


def cmd_detect(a) -> int:
    info = system.detect()
    if a.json:
        out_json(info.to_dict())
        return 0
    for k, v in info.to_dict().items():
        print(f"{k:<12} {', '.join(v) if isinstance(v, list) else v}")
    return 0


def apply_config(cfg: config.Config) -> list[str]:
    """Save a validated config and bring timers and rules in line with it."""
    config.save(cfg)
    return schedule.sync(cfg)


def _editor() -> list[str]:
    for var in ("SUDO_EDITOR", "VISUAL", "EDITOR"):
        if os.environ.get(var):
            return os.environ[var].split()
    for e in ("nano", "vim", "vi"):
        if shutil.which(e):
            return [e]
    raise LifeboatError("No editor found. Set EDITOR.")


def cmd_config(a) -> int:
    if a.action == "path":
        print(paths.CONFIG_FILE)
        return 0
    if a.action == "example":
        print(config.example(), end="")
        return 0
    if a.action == "show":
        if paths.CONFIG_FILE.exists():
            print(paths.CONFIG_FILE.read_text(), end="")
        else:
            print("# No config yet. Start from: lifeboat config example")
        return 0
    if a.action == "check":
        src = Path(a.file) if a.file else paths.CONFIG_FILE
        cfg = config.parse(src.read_text())
        errors = config.validate(cfg)
        if errors:
            raise config.ConfigError(errors)
        print(f"{src}: OK ({len(cfg.jobs)} job(s), {len(cfg.destinations)} destination(s))")
        return 0
    require_root("Changing the config")
    if a.action == "import":
        text = sys.stdin.read() if a.file in (None, "-") else Path(a.file).read_text()
        if text.lstrip().startswith("{"):
            cfg, errors = config.from_dict(json.loads(text))
            if errors:
                raise config.ConfigError(errors)
        else:
            cfg = config.parse(text)
        for change in apply_config(cfg):
            print(change)
        print("Config saved.")
        return 0
    if a.action == "edit":
        current = paths.CONFIG_FILE.read_text() if paths.CONFIG_FILE.exists() else config.example()
        fd, tmp = tempfile.mkstemp(prefix="lifeboat-config-", suffix=".toml")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(current)
            while True:
                subprocess.run(_editor() + [tmp])
                text = Path(tmp).read_text()
                if text == current and paths.CONFIG_FILE.exists():
                    print("No changes.")
                    return 0
                try:
                    cfg = config.parse(text)
                    errors = config.validate(cfg)
                    if errors:
                        raise config.ConfigError(errors)
                    for job in cfg.jobs.values():
                        if job.schedule.mode == "custom":
                            schedule.check_calendar(job.schedule.calendar)
                except LifeboatError as exc:
                    print(exc)
                    if input("Edit again? [Y/n] ").strip().lower() in ("n", "no"):
                        print("Nothing saved.")
                        return 1
                    continue
                # Keep the user's own formatting and comments: save the text they wrote.
                from .util import atomic_write
                atomic_write(paths.CONFIG_FILE, text, 0o644)
                for change in schedule.sync(cfg):
                    print(change)
                print("Config saved.")
                return 0
        finally:
            os.unlink(tmp)
    return 2


def cmd_schedule(a) -> int:
    cfg = config.load()
    if a.action == "cron":
        print(schedule.cron_lines(cfg), end="")
        return 0
    if a.action == "show":
        for job in cfg.jobs.values():
            cal = job.schedule.on_calendar()
            nxt = schedule.next_run(job.name)
            print(f"{job.name:<16} {job.schedule.describe():<32} {cal or '':<26} next: {fmt_time(nxt) if nxt else '-'}")
        return 0
    require_root("Updating schedules")
    changes = schedule.sync(cfg)
    print("\n".join(changes) if changes else "Schedules are up to date.")
    return 0


def cmd_hook(a) -> int:
    """Called by the pacman hook before every transaction. Never blocks pacman on failure."""
    try:
        cfg = config.load()
    except LifeboatError as exc:
        print(f"Lifeboat: config problem, no backup made: {exc}")
        return 0
    jobs = [j for j in cfg.jobs.values() if j.enabled and j.triggers.before_pacman]
    for job in jobs:
        print(f"Lifeboat: backing up '{job.name}' before the transaction...", flush=True)
        try:
            result = engine.run_job(job.name, "pacman")
        except LifeboatError as exc:
            print(f"Lifeboat: {exc}")
            continue
        print(f"Lifeboat: {STATUS_ICON.get(result['status'], result['status'])}: {result['message']}", flush=True)
    return 0


def cmd_log(a) -> int:
    require_root("Reading logs")
    d = paths.LOG_DIR / a.job
    logs = sorted(d.glob("*.log")) if d.is_dir() else []
    if a.list:
        for f in logs:
            print(f.name)
        return 0
    if not logs:
        print(f"No logs for '{a.job}'.")
        return 1
    target = d / a.name if a.name else logs[-1]
    if target.parent != d or not target.is_file():
        raise LifeboatError("No such log")
    print(target.read_text(errors="replace"), end="")
    return 0


def cmd_history(a) -> int:
    h = status.history(a.job, limit=a.limit)
    if a.json:
        out_json(h)
        return 0
    rows = [[fmt_time(e.get("started")), e.get("job", ""), e.get("action", ""), e.get("trigger", ""),
             STATUS_ICON.get(e.get("status"), e.get("status")), (e.get("message") or "")[:70]] for e in h]
    table(rows, ["WHEN", "JOB", "ACTION", "TRIGGER", "RESULT", "MESSAGE"])
    return 0


def cmd_helper(a) -> int:
    from . import helper
    return helper.serve()


# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="lifeboat", description=f"{APP_NAME}: snapshot backups for Arch Linux.")
    p.add_argument("-v", "--verbose", action="store_true", help="show detailed progress and debug output")
    p.add_argument("--version", action="version", version=f"lifeboat {__version__}")
    sub = p.add_subparsers(dest="command", metavar="COMMAND")

    def add(name, fn, help_, **kw):
        sp = sub.add_parser(name, help=help_, description=help_, **kw)
        sp.set_defaults(fn=fn)
        return sp

    sp = add("status", cmd_status, "show jobs, their last and next runs")
    sp.add_argument("--json", action="store_true")

    sp = add("run", cmd_run, "run a backup job now and wait for it")
    sp.add_argument("job")
    sp.add_argument("--trigger", default="manual", choices=["manual", "timer", "pacman", "drive"], help=argparse.SUPPRESS)
    sp.add_argument("--no-prune", action="store_true", help="don't remove old backups afterwards")
    sp.add_argument("--json", action="store_true")

    sp = add("start", cmd_start, "start a backup job in the background")
    sp.add_argument("job")
    sp = add("cancel", cmd_cancel, "stop a running backup, restore or prune")
    sp.add_argument("what", metavar="JOB_OR_KEY")

    sp = add("list", cmd_list, "list the backups (snapshots) of a job")
    sp.add_argument("job")
    sp.add_argument("--json", action="store_true")

    sp = add("show", cmd_show, "show details of one snapshot")
    sp.add_argument("job")
    sp.add_argument("snapshot")
    sp.add_argument("--json", action="store_true")

    sp = add("browse", cmd_browse, "list files inside a snapshot")
    sp.add_argument("job")
    sp.add_argument("snapshot")
    sp.add_argument("path", nargs="?", default="/")
    sp.add_argument("--meta", action="store_true", help="browse saved metadata (package lists, disk layout) instead")
    sp.add_argument("--json", action="store_true")

    sp = add("restore", cmd_restore, "restore files or folders from a snapshot")
    sp.add_argument("job")
    sp.add_argument("snapshot")
    sp.add_argument("paths", nargs="+", metavar="PATH", help="original absolute paths, e.g. /home/me/.config/kitty")
    sp.add_argument("--to", metavar="DIR", help="restore into this folder instead of the original location")
    sp.add_argument("--delete", action="store_true", help="also remove files that aren't in the backup (exact copy)")
    sp.add_argument("--dry-run", action="store_true", help="only show what would change")
    sp.add_argument("--force", action="store_true", help="allow restoring system folders like /usr over the running system")
    sp.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    sp.add_argument("--key", help=argparse.SUPPRESS)
    sp.add_argument("--json", action="store_true")

    sp = add("prune", cmd_prune, "remove backups the retention rules no longer keep")
    sp.add_argument("job")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--key", help=argparse.SUPPRESS)
    sp.add_argument("--json", action="store_true")

    sp = add("pin", lambda a: cmd_pin(a, True), "keep a snapshot forever (pruning skips it)")
    sp.add_argument("job")
    sp.add_argument("snapshot")
    sp.add_argument("--note", default="", help="why it's pinned")
    sp = add("unpin", lambda a: cmd_pin(a, False), "let pruning remove a snapshot again")
    sp.add_argument("job")
    sp.add_argument("snapshot")

    sp = add("delete", cmd_delete, "delete one snapshot")
    sp.add_argument("job")
    sp.add_argument("snapshot")
    sp.add_argument("-y", "--yes", action="store_true")

    sp = add("check", cmd_check, "test a destination: reachable, writable, what it can store")
    sp.add_argument("destination")
    sp.add_argument("--json", action="store_true")

    sp = add("drives", cmd_drives, "list drives that can hold backups")
    sp.add_argument("--json", action="store_true")
    sp = add("exports", cmd_exports, "list the NFS exports of a server")
    sp.add_argument("server")

    sp = add("modules", cmd_modules, "list what can be backed up")
    sp.add_argument("--json", action="store_true")
    sp = add("detect", cmd_detect, "show what Lifeboat detects about this system")
    sp.add_argument("--json", action="store_true")

    sp = add("config", cmd_config, "show, edit, check or import the configuration")
    sp.add_argument("action", choices=["show", "edit", "check", "import", "example", "path"])
    sp.add_argument("file", nargs="?", help="for check and import (- for stdin)")

    sp = add("schedule", cmd_schedule, "sync timers with the config, show schedules, or export cron lines")
    sp.add_argument("action", choices=["sync", "show", "cron"], nargs="?", default="show")

    sp = add("history", cmd_history, "show past runs")
    sp.add_argument("job", nargs="?")
    sp.add_argument("--limit", type=int, default=30)
    sp.add_argument("--json", action="store_true")

    sp = add("log", cmd_log, "show the log of a job's latest (or a given) run")
    sp.add_argument("job")
    sp.add_argument("name", nargs="?", help="log file name from --list")
    sp.add_argument("--list", action="store_true")

    # Internal commands, used by the pacman hook and the GUI. Kept out of --help.
    sp = sub.add_parser("hook")
    sp.set_defaults(fn=cmd_hook)
    sp.add_argument("which", choices=["pacman"])
    sp = sub.add_parser("helper")
    sp.set_defaults(fn=cmd_helper)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    a = parser.parse_args(argv)
    level = logging.DEBUG if a.verbose else (logging.INFO if not sys.stderr.isatty() else logging.WARNING)
    if getattr(a, "fn", None) is cmd_hook and not a.verbose:
        level = logging.ERROR  # pacman shows hook output; keep it to our one-line results
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s" if sys.stderr.isatty() else "%(levelname)s %(message)s"))
    handler.setLevel(level)
    log.addHandler(handler)
    log.setLevel(logging.DEBUG if a.verbose else logging.INFO)
    if not getattr(a, "fn", None):
        parser.print_help()
        return 2
    try:
        return a.fn(a) or 0
    except config.ConfigError as exc:
        print(f"lifeboat: {exc}", file=sys.stderr)
        return 1
    except LifeboatError as exc:
        print(f"lifeboat: {exc}", file=sys.stderr)
        return 75 if isinstance(exc, engine.Unavailable) else 1
    except PermissionError as exc:
        if exc.filename and os.geteuid() == 0:
            print(f"lifeboat: {destinations.permission_message(exc.filename)}", file=sys.stderr)
        else:
            print(f"lifeboat: permission denied: {exc.filename or exc}. Most commands need sudo.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0
