"""Decide which snapshots to keep.

Works like restic and borg: keep the newest N, then the newest snapshot in
each of the last N hours, days, ISO weeks, months and years. A snapshot kept
by any rule is kept. Pinned snapshots and the newest good snapshot are never
removed.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from .config import Retention
from .snapshots import Snapshot

BUCKETS = [
    ("hourly", lambda d: (d.year, d.month, d.day, d.hour)),
    ("daily", lambda d: (d.year, d.month, d.day)),
    ("weekly", lambda d: tuple(d.isocalendar())[:2]),
    ("monthly", lambda d: (d.year, d.month)),
    ("yearly", lambda d: (d.year,)),
]


@dataclass
class Decision:
    keep: dict[str, list[str]] = field(default_factory=dict)   # name -> reasons
    remove: list[Snapshot] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"keep": self.keep, "remove": [s.name for s in self.remove]}


def decide(snaps: list[Snapshot], r: Retention, now: float) -> Decision:
    """snaps may be in any order. Incomplete snapshots are left alone (never counted, never removed)."""
    good = sorted([s for s in snaps if s.status in ("success", "warning")],
                  key=lambda s: (s.time, s.name), reverse=True)
    dec = Decision()

    def keep(s: Snapshot, why: str) -> None:
        dec.keep.setdefault(s.name, []).append(why)

    if r.keep_all:
        for s in good:
            keep(s, "keep all")
        return dec

    for s in good:
        if s.pinned:
            keep(s, "pinned")
    if good:
        keep(good[0], "newest")
    for s in good[: r.keep_last]:
        keep(s, "last")
    for label, keyfn in BUCKETS:
        n = getattr(r, f"keep_{label}")
        if n <= 0:
            continue
        last_key = None
        for s in good:
            key = keyfn(dt.datetime.fromtimestamp(s.time))
            if key != last_key:
                keep(s, label)
                last_key = key
                n -= 1
                if n == 0:
                    break
    if r.keep_within_days > 0:
        cutoff = now - r.keep_within_days * 86400
        for s in good:
            if s.time >= cutoff:
                keep(s, f"within {r.keep_within_days}d")
    dec.remove = [s for s in good if s.name not in dec.keep]
    dec.remove.sort(key=lambda s: s.time)  # oldest first
    return dec


def space_victims(snaps: list[Snapshot]) -> list[Snapshot]:
    """Order in which the space guard may delete: oldest first, never pinned, never the newest."""
    good = sorted([s for s in snaps if s.status in ("success", "warning")], key=lambda s: s.time)
    if not good:
        return []
    newest = good[-1]
    return [s for s in good if not s.pinned and s is not newest]
