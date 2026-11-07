import os

from lifeboat import util
from lifeboat.config import Job
from lifeboat.modules import registry
from lifeboat.modules.base import Context
from lifeboat.system import SystemInfo, User


def ctx(tmp_path, modules, allow_secrets=False, include=None):
    home = tmp_path / "home" / "me"
    for d in (".config/kitty", ".config/chromium/Default/GPUCache", ".cache", ".ssh", "Documents"):
        (home / d).mkdir(parents=True, exist_ok=True)
    (home / ".bashrc").write_text("x")
    (home / ".Xauthority").write_text("x")
    job = Job(name="t", destination="d", modules=modules, include=include or [])
    return Context(job=job, users=[User("me", 1000, 1000, str(home))], info=SystemInfo(), allow_secrets=allow_secrets), home


def test_every_module_has_a_description():
    for m in registry.describe():
        assert m["id"] and m["name"] and len(m["summary"]) > 20


def test_presets_use_real_modules():
    for mods in registry.PRESETS.values():
        assert set(mods) <= set(registry.ids())


def test_dotfiles_skip_junk(tmp_path):
    c, home = ctx(tmp_path, ["dotfiles"])
    plan, _ = registry.build(c)
    assert str(home / ".bashrc") in plan.paths
    assert str(home / ".Xauthority") not in plan.paths


def test_configs_skip_browsers_unless_browsers_selected(tmp_path):
    c, home = ctx(tmp_path, ["configs"])
    plan, _ = registry.build(c)
    chromium = str(home / ".config/chromium")
    assert chromium in plan.excludes
    c2, _ = ctx(tmp_path, ["configs", "browsers"])
    plan2, _ = registry.build(c2)
    assert chromium not in plan2.excludes
    assert any("GPUCache" in e for e in plan2.excludes)


def test_secrets_need_encryption(tmp_path):
    c, home = ctx(tmp_path, ["secrets"])
    plan, _ = registry.build(c)
    assert not any(".ssh" in p for p in plan.paths)
    assert any("encrypted" in w for w in plan.warnings)
    c2, _ = ctx(tmp_path, ["secrets"], allow_secrets=True)
    plan2, _ = registry.build(c2)
    assert str(home / ".ssh") in plan2.paths


def test_destination_inside_sources_is_excluded(tmp_path):
    c, home = ctx(tmp_path, ["home"])
    dest = str(home / "Documents" / "backups")
    plan, _ = registry.build(c, dest_paths=[dest])
    assert dest in plan.excludes
    assert str(home / ".cache") in plan.excludes


def test_missing_extra_paths_warn(tmp_path):
    c, _ = ctx(tmp_path, [], include=[str(tmp_path / "nope")])
    plan, _ = registry.build(c)
    assert any("doesn't exist" in w for w in plan.warnings)


def test_minimize_paths():
    assert util.minimize_paths(["/a/b", "/a", "/c/d", "/c/de"]) == ["/a", "/c/d", "/c/de"]
    assert util.is_within("/a/b", "/a") and not util.is_within("/ab", "/a")
