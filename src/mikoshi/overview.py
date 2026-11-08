"""Job overview built from public data (config, history, timers). Needs no root."""

from __future__ import annotations

from . import config, schedule, status
from .modules import registry


def jobs(cfg: config.Config) -> list[dict]:
    running = {s.get("job"): s for s in status.running() if s.get("action") == "backup"}
    out = []
    for name, job in cfg.jobs.items():
        dest = cfg.destinations.get(job.destination)
        last = status.last_run(name)
        good = status.last_success(name)
        out.append({
            "name": name,
            "description": job.description,
            "enabled": job.enabled,
            "destination": job.destination,
            "destination_label": dest.describe() if dest else "missing",
            "destination_type": dest.type if dest else "",
            "modules": job.modules,
            "module_names": [registry.get(m).name for m in job.modules if m in registry.ids()],
            "schedule": job.schedule.describe() if job.enabled else "Turned off",
            "retention": job.retention.describe(),
            "before_pacman": job.triggers.before_pacman,
            "on_drive_connect": job.triggers.on_drive_connect,
            "next_run": schedule.next_run(name) if job.enabled and job.schedule.mode != "manual" else None,
            "last_run": last,
            "last_success": good,
            "running": running.get(name),
        })
    return out
