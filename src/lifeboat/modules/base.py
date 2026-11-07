"""Shared types for backup modules."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..config import Job
from ..system import SystemInfo, User

# A metadata task writes files into the given folder and returns warnings.
MetaTask = Callable[[Path], list[str]]


@dataclass
class Context:
    job: Job
    users: list[User]
    info: SystemInfo
    allow_secrets: bool = False   # destination is encrypted, or the user accepted the risk


@dataclass
class Plan:
    paths: list[str] = field(default_factory=list)          # absolute paths to copy
    excludes: list[str] = field(default_factory=list)       # rsync patterns, always applied
    soft_excludes: list[str] = field(default_factory=list)  # concrete paths, dropped if another module includes them
    meta: list[tuple[str, MetaTask]] = field(default_factory=list)  # (label, task)
    warnings: list[str] = field(default_factory=list)   # problems worth a notification
    notes: list[str] = field(default_factory=list)      # informational, logged only


class Module:
    id = ""
    name = ""
    summary = ""
    sensitive = False   # holds secrets: needs an encrypted destination
    heavy = False       # can be large or slow
    per_user = False

    def plan(self, ctx: Context) -> Plan:  # pragma: no cover - interface
        raise NotImplementedError

    def info(self) -> dict:
        return {"id": self.id, "name": self.name, "summary": self.summary,
                "sensitive": self.sensitive, "heavy": self.heavy, "per_user": self.per_user}


def existing(paths) -> list[str]:
    """Keep paths that exist (as files, dirs or symlinks)."""
    return [p for p in paths if os.path.lexists(p)]


def home_paths(user: User, rels) -> list[str]:
    return [os.path.join(user.home, r) for r in rels]
