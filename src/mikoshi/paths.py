"""Filesystem locations used by Mikoshi.

Every location can be redirected with an environment variable so the test
suite can run without touching the real system. pkexec and sudo clear the
environment, so these overrides never leak into privileged runs.
"""

import os
from pathlib import Path


def _p(env: str, default: str) -> Path:
    return Path(os.environ.get(env, default))


CONFIG_DIR = _p("MIKOSHI_CONFIG_DIR", "/etc/mikoshi")
CONFIG_FILE = CONFIG_DIR / "config.toml"
SECRETS_FILE = CONFIG_DIR / "secrets.toml"

STATE_DIR = _p("MIKOSHI_STATE_DIR", "/var/lib/mikoshi")
HISTORY_FILE = STATE_DIR / "history.jsonl"
UNDO_DIR = STATE_DIR / "undo"

RUN_DIR = _p("MIKOSHI_RUN_DIR", "/run/mikoshi")
STATUS_DIR = RUN_DIR / "status"
LOCK_DIR = RUN_DIR / "locks"
MOUNT_DIR = RUN_DIR / "mnt"

LOG_DIR = _p("MIKOSHI_LOG_DIR", "/var/log/mikoshi")

SYSTEMD_DIR = _p("MIKOSHI_SYSTEMD_DIR", "/etc/systemd/system")
UDEV_RULES_FILE = _p("MIKOSHI_UDEV_RULES", "/etc/udev/rules.d/90-mikoshi.rules")

# Name of the folder Mikoshi creates inside a destination.
REPO_DIRNAME = "mikoshi"

# Installed location of the CLI, used by generated units and pkexec.
BIN = os.environ.get("MIKOSHI_BIN", "/usr/bin/mikoshi")
