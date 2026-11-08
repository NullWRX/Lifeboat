"""Configuration: schema, loading, validation and saving.

The config lives in /etc/mikoshi/config.toml and is world-readable so the
GUI can show it without a password. Secrets (the ntfy access token) live in
/etc/mikoshi/secrets.toml, readable by root only.
"""

from __future__ import annotations

import dataclasses
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import paths, tomlw
from .util import MikoshiError, atomic_write

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
SCHEDULE_MODES = ["manual", "hourly", "daily", "weekly", "monthly", "custom"]
DEST_TYPES = ["folder", "drive", "nfs"]
NOTIFY_MODES = ["always", "failure", "never"]

HEADER = """Mikoshi configuration.
Edit with `sudo mikoshi config edit` (validates before saving) or the Mikoshi app.
Full reference: man mikoshi.conf(5)."""


@dataclass
class Settings:
    hostname: str = ""          # folder name used inside destinations; empty = system hostname
    notify_on: str = "failure"  # always | failure | never
    notify_desktop: bool = True
    ntfy_url: str = ""          # e.g. https://ntfy.sh/my-topic
    log_keep: int = 30          # log files kept per job


@dataclass
class Destination:
    name: str = ""
    type: str = "folder"        # folder | drive | nfs
    path: str = ""              # folder: absolute path
    uuid: str = ""              # drive: filesystem UUID (or LUKS container UUID)
    keyfile: str = ""           # drive: key file used to unlock a LUKS drive
    server: str = ""            # nfs: host name or IP
    export: str = ""            # nfs: exported path on the server
    options: str = ""           # nfs: extra mount options
    subdir: str = ""            # folder inside the drive or share to use
    encrypted: bool = False     # set when the storage is encrypted (LUKS drive, encrypted dataset)
    same_disk_ok: bool = False  # folder: allow a folder on the same disk as the system

    def describe(self) -> str:
        if self.type == "folder":
            return self.path
        if self.type == "drive":
            return f"Drive {self.uuid}" + (f" / {self.subdir}" if self.subdir else "")
        return f"{self.server}:{self.export}" + (f" / {self.subdir}" if self.subdir else "")


@dataclass
class Schedule:
    mode: str = "manual"        # manual | hourly | daily | weekly | monthly | custom
    time: str = "02:30"         # HH:MM (hourly uses only the minutes)
    weekday: str = "sun"        # weekly
    day: int = 1                # monthly, 1-28
    calendar: str = ""          # custom: systemd OnCalendar expression
    random_delay_min: int = 0   # spread start times

    def on_calendar(self) -> str | None:
        if self.mode == "manual":
            return None
        if self.mode == "custom":
            return self.calendar.strip()
        hh, mm = self.time.split(":")
        if self.mode == "hourly":
            return f"*-*-* *:{mm}:00"
        if self.mode == "daily":
            return f"*-*-* {hh}:{mm}:00"
        if self.mode == "weekly":
            return f"{self.weekday.capitalize()} *-*-* {hh}:{mm}:00"
        if self.mode == "monthly":
            return f"*-*-{self.day:02d} {hh}:{mm}:00"
        return None

    def cron(self) -> str | None:
        """Equivalent crontab time fields, or None when cron can't express it."""
        if self.mode in ("manual", "custom"):
            return None
        hh, mm = (int(x) for x in self.time.split(":"))
        if self.mode == "hourly":
            return f"{mm} * * * *"
        if self.mode == "daily":
            return f"{mm} {hh} * * *"
        if self.mode == "weekly":
            return f"{mm} {hh} * * {(WEEKDAYS.index(self.weekday) + 1) % 7}"
        if self.mode == "monthly":
            return f"{mm} {hh} {self.day} * *"
        return None

    def describe(self) -> str:
        if self.mode == "manual":
            return "Manual only"
        if self.mode == "hourly":
            return f"Every hour at :{self.time.split(':')[1]}"
        if self.mode == "daily":
            return f"Daily at {self.time}"
        if self.mode == "weekly":
            return f"Weekly on {self.weekday.capitalize()} at {self.time}"
        if self.mode == "monthly":
            return f"Monthly on day {self.day} at {self.time}"
        return f"Custom: {self.calendar}"


@dataclass
class Triggers:
    before_pacman: bool = False     # run before every pacman transaction
    on_drive_connect: bool = False  # run when the destination drive is plugged in
    min_interval_hours: int = 0     # event triggers skip if the last success is newer than this


@dataclass
class Retention:
    keep_last: int = 3
    keep_hourly: int = 0
    keep_daily: int = 7
    keep_weekly: int = 4
    keep_monthly: int = 6
    keep_yearly: int = 0
    keep_within_days: int = 0
    min_free_gb: int = 0           # prune oldest backups until this much space is free
    prune_after_backup: bool = True
    keep_all: bool = False         # never prune

    def any_rule(self) -> bool:
        return any(
            getattr(self, f) > 0
            for f in ("keep_last", "keep_hourly", "keep_daily", "keep_weekly",
                      "keep_monthly", "keep_yearly", "keep_within_days")
        )

    def describe(self) -> str:
        if self.keep_all:
            return "Keep everything"
        parts = []
        for label, n in (("last", self.keep_last), ("hourly", self.keep_hourly),
                         ("daily", self.keep_daily), ("weekly", self.keep_weekly),
                         ("monthly", self.keep_monthly), ("yearly", self.keep_yearly)):
            if n:
                parts.append(f"{n} {label}")
        if self.keep_within_days:
            parts.append(f"all from last {self.keep_within_days} days")
        return "Keep " + ", ".join(parts) if parts else "Keep everything"


@dataclass
class Job:
    name: str = ""
    destination: str = ""
    enabled: bool = True
    description: str = ""
    modules: list[str] = field(default_factory=list)
    users: list[str] = field(default_factory=list)      # empty = every regular user
    include: list[str] = field(default_factory=list)    # extra absolute paths
    exclude: list[str] = field(default_factory=list)    # rsync exclude patterns
    one_file_system: bool = True
    allow_unencrypted_secrets: bool = False
    allow_lossy: bool = False                           # allow destinations that can't keep owners
    pre_command: str = ""
    post_command: str = ""
    healthcheck_url: str = ""
    schedule: Schedule = field(default_factory=Schedule)
    triggers: Triggers = field(default_factory=Triggers)
    retention: Retention = field(default_factory=Retention)


@dataclass
class Config:
    settings: Settings = field(default_factory=Settings)
    destinations: dict[str, Destination] = field(default_factory=dict)
    jobs: dict[str, Job] = field(default_factory=dict)

    def job(self, name: str) -> Job:
        try:
            return self.jobs[name]
        except KeyError:
            raise MikoshiError(f"No job named '{name}'. Jobs: {', '.join(self.jobs) or 'none'}") from None

    def destination_for(self, job: Job) -> Destination:
        try:
            return self.destinations[job.destination]
        except KeyError:
            raise MikoshiError(
                f"Job '{job.name}' uses destination '{job.destination}', which doesn't exist."
            ) from None


@dataclass
class Secrets:
    ntfy_token: str = ""


# ---------------------------------------------------------------------------
# dict <-> dataclass

class ConfigError(MikoshiError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("Invalid configuration:\n  - " + "\n  - ".join(errors))


def _fill(cls, data: Any, where: str, errors: list[str]):
    """Build dataclass cls from data, recording type errors and unknown keys."""
    obj = cls()
    if data is None:
        return obj
    if not isinstance(data, dict):
        errors.append(f"{where}: expected a table")
        return obj
    fields = {f.name: f for f in dataclasses.fields(cls)}
    for key, value in data.items():
        if key == "name" and cls in (Job, Destination):
            continue
        f = fields.get(key)
        if f is None:
            errors.append(f"{where}.{key}: unknown setting")
            continue
        default = getattr(obj, key)
        if dataclasses.is_dataclass(default):
            setattr(obj, key, _fill(type(default), value, f"{where}.{key}", errors))
        elif isinstance(default, bool):
            if isinstance(value, bool):
                setattr(obj, key, value)
            else:
                errors.append(f"{where}.{key}: expected true or false")
        elif isinstance(default, int):
            if isinstance(value, int) and not isinstance(value, bool):
                setattr(obj, key, value)
            else:
                errors.append(f"{where}.{key}: expected a whole number")
        elif isinstance(default, str):
            if isinstance(value, str):
                setattr(obj, key, value)
            else:
                errors.append(f"{where}.{key}: expected text")
        elif isinstance(default, list):
            if isinstance(value, list) and all(isinstance(x, str) for x in value):
                setattr(obj, key, list(value))
            else:
                errors.append(f"{where}.{key}: expected a list of text values")
    return obj


def from_dict(data: dict) -> tuple[Config, list[str]]:
    errors: list[str] = []
    cfg = Config()
    for key in data:
        if key not in ("settings", "destinations", "jobs"):
            errors.append(f"{key}: unknown section")
    cfg.settings = _fill(Settings, data.get("settings"), "settings", errors)
    dests = data.get("destinations") or {}
    if not isinstance(dests, dict):
        errors.append("destinations: expected a table")
        dests = {}
    for name, d in dests.items():
        dest = _fill(Destination, d, f"destinations.{name}", errors)
        dest.name = name
        cfg.destinations[name] = dest
    jobs = data.get("jobs") or {}
    if not isinstance(jobs, dict):
        errors.append("jobs: expected a table")
        jobs = {}
    for name, j in jobs.items():
        job = _fill(Job, j, f"jobs.{name}", errors)
        job.name = name
        cfg.jobs[name] = job
    return cfg, errors


_DEST_FIELDS = {
    "folder": ["path", "same_disk_ok"],
    "drive": ["uuid", "keyfile", "subdir"],
    "nfs": ["server", "export", "options", "subdir"],
}


def to_dict(cfg: Config) -> dict:
    out: dict[str, Any] = {"settings": dataclasses.asdict(cfg.settings), "destinations": {}, "jobs": {}}
    for name, d in cfg.destinations.items():
        entry = {"type": d.type}
        for f in _DEST_FIELDS.get(d.type, []):
            entry[f] = getattr(d, f)
        entry["encrypted"] = d.encrypted
        out["destinations"][name] = entry
    for name, j in cfg.jobs.items():
        entry = dataclasses.asdict(j)
        entry.pop("name")
        # Keep nested tables last so the TOML reads naturally.
        for sub in ("schedule", "triggers", "retention"):
            entry[sub] = entry.pop(sub)
        out["jobs"][name] = entry
    return out


# ---------------------------------------------------------------------------
# validation

def validate(cfg: Config, module_ids: set[str] | None = None) -> list[str]:
    """Check everything a run would trip over. Returns a list of problems."""
    if module_ids is None:
        from .modules import registry
        module_ids = set(registry.ids())
    errors: list[str] = []
    s = cfg.settings
    if s.hostname and not NAME_RE.match(s.hostname.lower()):
        errors.append("settings.hostname: use letters, digits, '-' or '_' (max 32)")
    if s.notify_on not in NOTIFY_MODES:
        errors.append(f"settings.notify_on: must be one of {', '.join(NOTIFY_MODES)}")
    if s.ntfy_url and not s.ntfy_url.startswith(("http://", "https://")):
        errors.append("settings.ntfy_url: must start with http:// or https://")
    if s.log_keep < 1:
        errors.append("settings.log_keep: must be at least 1")

    for name, d in cfg.destinations.items():
        w = f"destinations.{name}"
        if not NAME_RE.match(name):
            errors.append(f"{w}: name must be lowercase letters, digits, '-' or '_' (max 32)")
        if d.type not in DEST_TYPES:
            errors.append(f"{w}.type: must be one of {', '.join(DEST_TYPES)}")
            continue
        if d.subdir and (d.subdir.startswith("/") or ".." in Path(d.subdir).parts):
            errors.append(f"{w}.subdir: must be a relative folder without '..'")
        if d.type == "folder":
            if not d.path.startswith("/"):
                errors.append(f"{w}.path: must be an absolute path")
            elif os.path.normpath(d.path) in ("/", "/proc", "/sys", "/dev", "/run", "/tmp"):
                errors.append(f"{w}.path: {d.path} can't hold backups")
        elif d.type == "drive":
            if not re.match(r"^[A-Za-z0-9-]{4,64}$", d.uuid):
                errors.append(f"{w}.uuid: expected a filesystem UUID")
            if d.keyfile and not d.keyfile.startswith("/"):
                errors.append(f"{w}.keyfile: must be an absolute path")
        elif d.type == "nfs":
            if not re.match(r"^[A-Za-z0-9.:\[\]-]+$", d.server):
                errors.append(f"{w}.server: expected a host name or IP address")
            if not d.export.startswith("/"):
                errors.append(f"{w}.export: must be an absolute path on the server")
            if d.options and not re.match(r"^[A-Za-z0-9_=.,:/-]+$", d.options):
                errors.append(f"{w}.options: unexpected characters")

    for name, j in cfg.jobs.items():
        w = f"jobs.{name}"
        if not NAME_RE.match(name):
            errors.append(f"{w}: name must be lowercase letters, digits, '-' or '_' (max 32)")
        if j.destination not in cfg.destinations:
            errors.append(f"{w}.destination: no destination named '{j.destination}'")
        if not j.modules and not j.include:
            errors.append(f"{w}: choose at least one thing to back up")
        for m in j.modules:
            if m not in module_ids:
                errors.append(f"{w}.modules: unknown module '{m}'")
        for p in j.include:
            if not p.startswith("/"):
                errors.append(f"{w}.include: '{p}' must be an absolute path")
        for u in j.users:
            if not re.match(r"^[a-z_][a-z0-9_.-]*\$?$", u):
                errors.append(f"{w}.users: '{u}' is not a valid user name")
        for pat in j.exclude:
            if not pat.strip() or "\n" in pat:
                errors.append(f"{w}.exclude: empty or multi-line pattern")
        if j.healthcheck_url and not j.healthcheck_url.startswith(("http://", "https://")):
            errors.append(f"{w}.healthcheck_url: must start with http:// or https://")

        sc = j.schedule
        if sc.mode not in SCHEDULE_MODES:
            errors.append(f"{w}.schedule.mode: must be one of {', '.join(SCHEDULE_MODES)}")
        if not TIME_RE.match(sc.time):
            errors.append(f"{w}.schedule.time: use 24-hour HH:MM")
        if sc.weekday not in WEEKDAYS:
            errors.append(f"{w}.schedule.weekday: one of {', '.join(WEEKDAYS)}")
        if not 1 <= sc.day <= 28:
            errors.append(f"{w}.schedule.day: 1 to 28 (so every month has it)")
        if sc.mode == "custom" and not sc.calendar.strip():
            errors.append(f"{w}.schedule.calendar: required for a custom schedule")
        if sc.calendar and ("\n" in sc.calendar or len(sc.calendar) > 200):
            errors.append(f"{w}.schedule.calendar: invalid expression")
        if not 0 <= sc.random_delay_min <= 24 * 60:
            errors.append(f"{w}.schedule.random_delay_min: 0 to 1440")

        if j.triggers.min_interval_hours < 0:
            errors.append(f"{w}.triggers.min_interval_hours: can't be negative")
        if j.triggers.on_drive_connect:
            d = cfg.destinations.get(j.destination)
            if d and d.type != "drive":
                errors.append(f"{w}.triggers.on_drive_connect: only works with a drive destination")

        r = j.retention
        for f in ("keep_last", "keep_hourly", "keep_daily", "keep_weekly", "keep_monthly",
                  "keep_yearly", "keep_within_days", "min_free_gb"):
            if getattr(r, f) < 0:
                errors.append(f"{w}.retention.{f}: can't be negative")
        if not r.keep_all and not r.any_rule():
            errors.append(f"{w}.retention: set at least one keep rule, or keep_all = true")
    return errors


# ---------------------------------------------------------------------------
# load / save

def parse(text: str) -> Config:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError([f"TOML syntax: {exc}"]) from exc
    cfg, errors = from_dict(data)
    if errors:
        raise ConfigError(errors)
    return cfg


def load(path: Path | None = None, *, check: bool = True) -> Config:
    path = path or paths.CONFIG_FILE
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return Config()
    except PermissionError as exc:
        raise MikoshiError(f"Can't read {path}: permission denied") from exc
    cfg = parse(text)
    if check:
        errors = validate(cfg)
        if errors:
            raise ConfigError(errors)
    return cfg


def dumps(cfg: Config) -> str:
    return tomlw.dumps(to_dict(cfg), header=HEADER)


def save(cfg: Config, path: Path | None = None) -> None:
    errors = validate(cfg)
    if errors:
        raise ConfigError(errors)
    path = path or paths.CONFIG_FILE
    atomic_write(path, dumps(cfg), mode=0o644)


def load_secrets() -> Secrets:
    try:
        data = tomllib.loads(paths.SECRETS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, PermissionError, tomllib.TOMLDecodeError):
        return Secrets()
    errors: list[str] = []
    return _fill(Secrets, data, "secrets", errors)


def save_secrets(sec: Secrets) -> None:
    atomic_write(paths.SECRETS_FILE, tomlw.dumps(dataclasses.asdict(sec), header="Mikoshi secrets (root only)."), mode=0o600)


def example() -> str:
    """A commented starter config used by `mikoshi config edit` on first run."""
    return EXAMPLE


EXAMPLE = """# Mikoshi configuration.
# Edit with `sudo mikoshi config edit` (validates before saving) or the Mikoshi app.
# Full reference: man mikoshi.conf(5).

[settings]
notify_on = "failure"        # always | failure | never
notify_desktop = true
ntfy_url = ""                # e.g. "https://ntfy.sh/my-topic"

# Where backups go. Types: folder, drive, nfs.
[destinations.usb]
type = "drive"
uuid = "PUT-FILESYSTEM-UUID-HERE"   # see `mikoshi drives`
encrypted = false

# [destinations.nas]
# type = "nfs"
# server = "192.168.1.50"
# export = "/mnt/pool/backups"

[jobs.daily]
destination = "usb"
modules = ["etc", "dotfiles", "configs", "packages", "disk-layout"]   # see `mikoshi modules`
users = []                   # empty = every regular user
exclude = []

[jobs.daily.schedule]
mode = "daily"               # manual | hourly | daily | weekly | monthly | custom
time = "02:30"

[jobs.daily.triggers]
before_pacman = false

[jobs.daily.retention]
keep_last = 3
keep_daily = 7
keep_weekly = 4
keep_monthly = 6
"""
