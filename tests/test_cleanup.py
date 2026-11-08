"""Old backup locations get tidied: empty leftovers removed, real backups only on request."""

import copy
import os

import pytest

from conftest import needs_root, needs_rsync
from mikoshi import cleanup, cli, config, engine, snapshots


def two_dest_config(a, b) -> config.Config:
    cfg = config.Config()
    cfg.destinations["a"] = config.Destination(name="a", type="folder", path=str(a), same_disk_ok=True)
    cfg.destinations["b"] = config.Destination(name="b", type="folder", path=str(b), same_disk_ok=True)
    cfg.jobs["j1"] = config.Job(name="j1", destination="a", include=["/etc/hostname"])
    cfg.jobs["j2"] = config.Job(name="j2", destination="a", include=["/etc/hostname"])
    return cfg


def keys(stale):
    return {(s.destination.name, tuple(s.jobs) if s.jobs is not None else None) for s in stale}


def test_find_stale_cases(tmp_path):
    old = two_dest_config(tmp_path / "a", tmp_path / "b")
    assert cleanup.find_stale(old, copy.deepcopy(old)) == []

    moved = copy.deepcopy(old)
    moved.jobs["j1"].destination = "b"
    assert keys(cleanup.find_stale(old, moved)) == {("a", ("j1",))}

    deleted_job = copy.deepcopy(old)
    del deleted_job.jobs["j2"]
    assert keys(cleanup.find_stale(old, deleted_job)) == {("a", ("j2",))}

    repointed = copy.deepcopy(old)
    repointed.destinations["a"].path = str(tmp_path / "elsewhere")
    assert keys(cleanup.find_stale(old, repointed)) == {("a", None)}

    deleted_dest = copy.deepcopy(old)
    for j in deleted_dest.jobs.values():
        j.destination = "b"
    del deleted_dest.destinations["a"]
    assert keys(cleanup.find_stale(old, deleted_dest)) == {("a", None)}


@needs_root
@needs_rsync
def test_switching_destination(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    cfg = two_dest_config(a, b)
    config.save(cfg)
    for j in cfg.jobs.values():
        j.retention = config.Retention(keep_last=5)
    config.save(cfg)
    assert engine.run_job("j1")["status"] == "success"
    host = next(p for p in (a / "mikoshi").iterdir() if p.is_dir()).name
    # Another PC sharing the destination must never be touched.
    (a / "mikoshi" / "otherpc" / "x").mkdir(parents=True)
    # j2 has an empty folder (all its backups were deleted by hand).
    (a / "mikoshi" / host / "j2" / "snapshots").mkdir(parents=True)

    new = config.load()
    new.jobs["j1"].destination = "b"
    new.jobs["j2"].destination = "b"
    changes, reports = cli.apply_config(new)
    assert len(reports) == 1
    r = reports[0]
    assert r.removed == ["j2"]                       # empty: removed automatically
    assert [j["job"] for j in r.jobs] == ["j1"] and r.has_data   # real backups: only reported
    assert (a / "mikoshi" / host / "j1").is_dir()

    gone = cleanup.process(config.Destination(**r.destination), host, ["j1"], delete_backups=True)
    assert gone.removed == ["j1"]
    assert not (a / "mikoshi" / host).exists()
    assert (a / "mikoshi" / "otherpc").is_dir()    # other PC's folder kept, so mikoshi/ stays


@needs_root
def test_test_destination_leaves_nothing(tmp_path):
    d = config.Destination(name="t", type="folder", path=str(tmp_path), same_disk_ok=True)
    res = cli.check_destination(d, "pc")
    assert res["ok"], res
    assert not (tmp_path / "mikoshi").exists()


@needs_root
@needs_rsync
def test_cli_cleanup_protects_jobs_in_use(tmp_path, capsys):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    cfg = two_dest_config(a, b)
    config.save(cfg)
    engine.run_job("j1")
    host = next(p for p in (a / "mikoshi").iterdir() if p.is_dir()).name
    (a / "mikoshi" / host / "oldjob" / "snapshots").mkdir(parents=True)
    assert cli.main(["cleanup", "--delete-backups", "--yes", str(a)]) == 0
    out = capsys.readouterr().out
    assert "Removed: oldjob" in out and "Kept j1: still backing up here" in out
    assert (a / "mikoshi" / host / "j1").is_dir()
    assert not (a / "mikoshi" / host / "oldjob").exists()
    with pytest.raises(SystemExit):
        cli.main(["cleanup"])
    assert snapshots.list_snapshots(a / "mikoshi" / host / "j1")
    assert os.path.exists(a / "mikoshi" / "README.txt")
