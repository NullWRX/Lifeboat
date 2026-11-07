"""Filesystem locations used by Lifeboat.

Every location can be redirected with an environment variable so the test
suite can run without touching the real system. pkexec and sudo clear the
environment, so these overrides never leak into privileged runs.
"""

import os
from pathlib import Path


def _p(env: str, default: str) -> Path:
    return Path(os.environ.get(env, default))


CONFIG_DIR = _p("LIFEBOAT_CONFIG_DIR", "/etc/lifeboat")
CONFIG_FILE = CONFIG_DIR / "config.toml"
SECRETS_FILE = CONFIG_DIR / "secrets.toml"

STATE_DIR = _p("LIFEBOAT_STATE_DIR", "/var/lib/lifeboat")
HISTORY_FILE = STATE_DIR / "history.jsonl"
UNDO_DIR = STATE_DIR / "undo"

RUN_DIR = _p("LIFEBOAT_RUN_DIR", "/run/lifeboat")
STATUS_DIR = RUN_DIR / "status"
LOCK_DIR = RUN_DIR / "locks"
MOUNT_DIR = RUN_DIR / "mnt"

LOG_DIR = _p("LIFEBOAT_LOG_DIR", "/var/log/lifeboat")

SYSTEMD_DIR = _p("LIFEBOAT_SYSTEMD_DIR", "/etc/systemd/system")
UDEV_RULES_FILE = _p("LIFEBOAT_UDEV_RULES", "/etc/udev/rules.d/90-lifeboat.rules")

# Name of the folder Lifeboat creates inside a destination.
REPO_DIRNAME = "lifeboat"

# Installed location of the CLI, used by generated units and pkexec.
BIN = os.environ.get("LIFEBOAT_BIN", "/usr/bin/lifeboat")
