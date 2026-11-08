"""Finding existing backups, continuing them after a reinstall, renaming the PC, name collisions."""

import os

import pytest

from conftest import needs_root, needs_rsync
from mikoshi import adopt, cli, config, engine, identity, snapshots

pytestmark = [needs_root, needs_rsync]


@pytest.fixture
def nas(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    (src / "f.txt").write_text("data\n")
    nas = tmp_path / "nas"
    nas.mkdir()
    monkeypatch.setattr(identity, "machine_tag", lambda: "machine-A")
    cfg = config.Config()
    cfg.settings.hostname = "nspc"
    cfg.destinations["nas"] = config.Destination(name="nas", type="folder", path=str(nas), same_disk_ok=True)
    job = config.Job(name="daily", destination="nas", include=[str(src)], pre_command="echo before",
                     modules=["dotfiles"], exclude=["*.tmp"])
    job.schedule = config.Schedule(mode="daily", time="04:15")
    job.retention = config.Retention(keep_last=2, keep_daily=9)
    cfg.jobs["daily"] = job
    config.save(cfg)
    assert engine.run_job("daily")["status"] == "success"
    return nas, src


def fresh_install(nas, monkeypatch, hostname="nspc"):
    """A reinstalled PC: new machine-id, only the destination configured."""
    monkeypatch.setattr(identity, "machine_tag", lambda: "machine-B")
    cfg = config.Config()
    cfg.settings.hostname = hostname
    cfg.destinations["nas"] = config.Destination(name="nas", type="folder", path=str(nas), same_disk_ok=True)
    config.save(cfg)
    return cfg


def test_marker_files(nas):
    nas_dir, _ = nas
    host = nas_dir / "mikoshi" / "nspc"
    info = identity.read_host(host)
    assert info["name"] == "nspc" and info["machines"] == ["machine-A"] and "daily" in info["jobs"]
    saved = identity.read_host(host / "daily")  # same reader works for any json
    assert not saved  # host.json only at PC level
    import json
    job = json.loads((host / "daily" / "job.json").read_text())["job"]
    assert job["schedule"]["time"] == "04:15" and job["retention"]["keep_daily"] == 9


def test_reinstall_finds_and_continues_backups(nas, monkeypatch):
    nas_dir, src = nas
    cfg = fresh_install(nas_dir, monkeypatch)
    found = adopt.find(cfg.destinations["nas"], "nspc")
    assert [p["name"] for p in found["pcs"]] == ["nspc"]
    pc = found["pcs"][0]
    assert not pc["this_machine"] and pc["jobs"][0]["name"] == "daily" and pc["jobs"][0]["snapshots"] == 1

    # Before adopting, a same-named job on the new machine is refused instead of mixing backups.
    cfg.jobs["daily"] = config.Job(name="daily", destination="nas", include=[str(src)])
    config.save(cfg)
    r = engine.run_job("daily")
    assert r["status"] == "failed" and "Another computer" in r["message"]
    del cfg.jobs["daily"]
    config.save(cfg)

    new, imported, skipped, notes, _ = adopt.use_backups(cfg, "nas", "nspc", None, "nspc")
    assert imported == ["daily"] and not skipped
    job = new.jobs["daily"]
    assert job.enabled is False                      # safe default after a reinstall
    assert job.pre_command == "" and any("before" in n for n in notes)  # root commands not imported
    assert job.schedule.time == "04:15" and job.retention.keep_daily == 9 and job.exclude == ["*.tmp"]
    cli.apply_config(new)
    r = engine.run_job("daily")                      # manual run works even while turned off
    assert r["status"] == "success", r
    jd = nas_dir / "mikoshi" / "nspc" / "daily"
    snaps = snapshots.list_snapshots(jd)
    assert len(snaps) == 2
    rel = str(src / "f.txt").lstrip("/")
    assert os.stat(snaps[0].files / rel).st_ino == os.stat(snaps[1].files / rel).st_ino  # chain continues
    assert set(identity.read_host(jd.parent)["machines"]) == {"machine-A", "machine-B"}


def test_adopting_another_name_and_renaming(nas, monkeypatch):
    nas_dir, _ = nas
    cfg = fresh_install(nas_dir, monkeypatch, hostname="archlinux")
    new, imported, _, _, _ = adopt.use_backups(cfg, "nas", "nspc", None, "archlinux")
    assert new.settings.hostname == "nspc" and imported == ["daily"]
    cli.apply_config(new)
    assert engine.run_job("daily")["status"] == "success"

    renamed = config.load()
    renamed.settings.hostname = "desktop"
    changes, _ = cli.apply_config(renamed)
    assert any("Moved this PC's backups" in c for c in changes)
    assert not (nas_dir / "mikoshi" / "nspc").exists()
    assert len(snapshots.list_snapshots(nas_dir / "mikoshi" / "desktop" / "daily")) == 2
    assert identity.read_host(nas_dir / "mikoshi" / "desktop")["name"] == "desktop"


def test_check_reports_collision(nas, monkeypatch):
    nas_dir, _ = nas
    cfg = fresh_install(nas_dir, monkeypatch)
    res = cli.check_destination(cfg.destinations["nas"], "nspc")
    assert not res["ok"] and "Another computer" in res["message"]
    assert res["found"][0]["name"] == "nspc"
    assert (nas_dir / "mikoshi" / "nspc" / "daily").is_dir()   # testing never removes real backups


def test_cli_adopt_records_history(nas, monkeypatch, capsys):
    from mikoshi import status
    nas_dir, _ = nas
    fresh_install(nas_dir, monkeypatch)
    assert cli.main(["find-backups", "nas"]) == 0
    assert "daily" in capsys.readouterr().out
    assert cli.main(["adopt", "--yes", "nas", "nspc"]) == 0
    out = capsys.readouterr().out
    assert "Imported job 'daily' (turned off)" in out
    last = status.last_success("daily")
    assert last and last["trigger"] == "imported"
