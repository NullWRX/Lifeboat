"""Use backups that already exist at a destination, and move them when this PC is renamed."""

from __future__ import annotations

import os

from . import cleanup, config, destinations, identity, paths
from .util import LifeboatError, Unavailable


def find(dest: config.Destination, this_host: str) -> dict:
    """Every PC folder with backups at a destination."""
    with destinations.open_destination(dest, this_host, create=False) as opened:
        pcs = identity.discover(opened.base)
        return {"location": str(opened.base / paths.REPO_DIRNAME), "this_pc": this_host, "pcs": pcs}


def record_imported(dest_name: str, pc: str, found: list[dict]) -> None:
    """Seed this PC's history with each adopted job's newest backup, so the app shows it."""
    import time
    from . import status
    for j in found:
        if j.get("newest"):
            status.append_history({
                "job": j["name"], "action": "backup", "trigger": "imported", "status": "success",
                "started": j["newest"], "finished": j["newest"], "duration": 0, "snapshot": None, "log": None,
                "message": f"Found {j['snapshots']} backup{'s' if j['snapshots'] != 1 else ''} from '{pc}' "
                           f"at {dest_name}, recorded {time.strftime('%Y-%m-%d')}.",
                "stats": {}, "warning_count": 0})


def use_backups(cfg: config.Config, dest_name: str, pc: str, jobs: list[str] | None,
                this_host: str) -> tuple[config.Config, list[str], list[str], list[str], list[dict]]:
    """Recreate jobs from a PC folder and make this PC continue it.

    Returns (new config, imported job names, skipped messages, notes, details of the imported
    jobs for record_imported()). Also marks the folder as belonging to this computer.
    """
    if not config.NAME_RE.match(pc):
        raise LifeboatError(f"'{pc}' isn't a valid PC name")
    dest = cfg.destinations.get(dest_name)
    if dest is None:
        raise LifeboatError(f"No destination named '{dest_name}'")
    new = config.parse(config.dumps(cfg))  # deep copy
    imported, skipped, notes = [], [], []
    imported_info: list[dict] = []
    with destinations.open_destination(dest, this_host, create=False) as opened:
        host_dir = opened.base / paths.REPO_DIRNAME / pc
        if not host_dir.is_dir():
            raise LifeboatError(f"No backups from '{pc}' at {dest_name}")
        found = {j["name"]: j for j in identity.describe_host(host_dir)["jobs"]}
        for name in (jobs if jobs is not None else list(found)):
            if name not in found:
                skipped.append(f"'{name}': no backups for it here")
                continue
            if name in new.jobs:
                skipped.append(f"'{name}': a job with this name already exists. Delete or rename it first.")
                continue
            job, job_notes = identity.imported_job(found[name]["config"], name, dest_name)
            new.jobs[name] = job
            imported.append(name)
            imported_info.append(found[name])
            notes += job_notes
            if found[name]["guessed"]:
                notes.append(f"'{name}' was made by an older Lifeboat, so only what it backs up was recovered. "
                             "Check its schedule and how many backups it keeps.")
        if pc != this_host:
            new.settings.hostname = "" if pc == _system_host() else pc
        errors = config.validate(new)
        if errors:
            raise config.ConfigError(errors)
        identity.claim(host_dir, pc)
    return new, imported, skipped, notes, imported_info


def _system_host() -> str:
    from . import system
    return system.hostname()


def move_host(cfg: config.Config, old: str, new: str) -> tuple[list[str], list]:
    """This PC's name changed: move its folder at every destination so backups follow.

    The caller makes sure nothing is running (a moved folder would break a running backup).
    """
    messages, reports = [], []
    seen = set()
    for dest in cfg.destinations.values():
        key = cleanup.location_key(dest)
        if key in seen:
            continue
        seen.add(key)
        try:
            with destinations.open_destination(dest, new, create=False) as opened:
                top = opened.base / paths.REPO_DIRNAME
                o, n = top / old, top / new
                if not o.is_dir():
                    continue
                if n.exists():
                    reports.append(cleanup.process(dest, old, None))
                    continue
                try:
                    identity.verify(o, old)
                except identity.OtherComputer:
                    messages.append(f"{dest.name}: '{old}' there belongs to another computer, left as is.")
                    continue
                os.rename(o, n)
                identity.claim(n, new)
                messages.append(f"Moved this PC's backups on {dest.name} from '{old}' to '{new}'.")
        except (Unavailable, LifeboatError) as exc:
            messages.append(f"Couldn't reach {dest.name} ({exc}). Backups there stay under '{old}'; "
                            "rename again once it's connected, or move the folder by hand.")
    return messages, reports
