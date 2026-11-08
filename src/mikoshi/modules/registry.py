"""All available modules, in display order, and plan merging."""

from __future__ import annotations

import os

from ..util import dedupe, is_within, minimize_paths
from .base import Context, Module, Plan
from .builtin import (BrowsersModule, ConfigsModule, DesktopModule, DiskLayoutModule, DotfilesModule,
                      EtcModule, HomeModule, PackagesModule, SecretsModule, ServicesModule, SystemModule)

MODULES: list[Module] = [
    EtcModule(), PackagesModule(), DiskLayoutModule(), ServicesModule(),
    DotfilesModule(), ConfigsModule(), DesktopModule(), HomeModule(),
    BrowsersModule(), SecretsModule(), SystemModule(),
]
_BY_ID = {m.id: m for m in MODULES}

# Sensible starting points offered by the GUI and `mikoshi job` presets.
PRESETS = {
    "essentials": ["etc", "packages", "disk-layout", "services", "dotfiles", "configs", "desktop"],
    "home": ["dotfiles", "configs", "desktop", "home", "browsers"],
    "everything": ["system", "packages", "disk-layout", "services"],
}


def ids() -> list[str]:
    return [m.id for m in MODULES]


def get(module_id: str) -> Module:
    return _BY_ID[module_id]


def describe() -> list[dict]:
    return [m.info() for m in MODULES]


def build(ctx: Context, dest_paths: list[str] | None = None) -> tuple[Plan, dict]:
    """Merge the plans of every selected module (plus extra paths) into one rsync plan.

    Returns the merged plan and a dict of module id -> meta tasks so each module's
    metadata lands in its own folder.
    """
    merged = Plan()
    soft: list[tuple[str, str]] = []   # (module id, path)
    path_owner: list[tuple[str, str]] = []
    meta: dict[str, list] = {}
    for mid in ctx.job.modules:
        mod = _BY_ID.get(mid)
        if mod is None:
            continue
        if mod.sensitive and not ctx.allow_secrets:
            merged.warnings.append(f"Skipped '{mod.name}': the destination isn't marked as encrypted.")
            continue
        p = mod.plan(ctx)
        path_owner += [(mid, x) for x in p.paths]
        merged.excludes += p.excludes
        soft += [(mid, x) for x in p.soft_excludes]
        if p.meta:
            meta[mid] = p.meta
        merged.warnings += p.warnings
        merged.notes += p.notes

    for x in ctx.job.include:
        if os.path.lexists(x):
            path_owner.append(("include", x))
        else:
            merged.warnings.append(f"Extra path {x} doesn't exist, skipped.")

    all_paths = [x for _, x in path_owner]
    # A soft exclude is dropped when a different module explicitly wants something at or below it.
    for owner, ex in soft:
        conflict = any(o != owner and is_within(p, ex) for o, p in path_owner)
        if not conflict:
            merged.excludes.append(ex)

    # Never copy the backup into itself.
    for d in dest_paths or []:
        if any(is_within(d, p) for p in all_paths):
            merged.excludes.append(d)

    merged.paths = minimize_paths(all_paths)
    merged.excludes = dedupe(merged.excludes + list(ctx.job.exclude))
    merged.warnings = dedupe(merged.warnings)
    merged.notes = dedupe(merged.notes)
    return merged, meta
