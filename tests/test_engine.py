"""End-to-end runs with real rsync. These need root (owners are preserved)."""

import os
import time

import pytest

from conftest import needs_root, needs_rsync
from mikoshi import config, engine, restore, snapshots, status
from mikoshi.util import MikoshiError

pytestmark = [needs_root, needs_rsync]


@pytest.fixture
def setup(tmp_path):
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "keep.txt").write_text("unchanged\n")
    (src / "edit.txt").write_text("version 1\n")
    (src / "sub" / "deep.txt").write_text("deep\n")
    os.chown(src / "edit.txt", 1234, 5678)
    dest = tmp_path / "dest"
    dest.mkdir()
    cfg = config.Config()
    cfg.destinations["d"] = config.Destination(name="d", type="folder", path=str(dest), same_disk_ok=True)
    job = config.Job(name="t", destination="d", include=[str(src)], exclude=["*.skip"])
    job.retention = config.Retention(keep_last=2, keep_daily=0, keep_weekly=0, keep_monthly=0)
    cfg.jobs["t"] = job
    config.save(cfg)
    return src, dest


def jobdir(dest):
    hosts = list((dest / "mikoshi").iterdir())
    hosts = [h for h in hosts if h.is_dir()]
    return hosts[0] / "t"


def test_backup_hardlinks_prune_and_restore(setup, tmp_path):
    src, dest = setup
    (src / "junk.skip").write_text("excluded")
    r1 = engine.run_job("t")
    assert r1["status"] == "success", r1
    jd = jobdir(dest)
    s1 = snapshots.get(jd, r1["snapshot"])
    copy = s1.files / str(src).lstrip("/")
    assert (copy / "keep.txt").read_text() == "unchanged\n"
    assert not (copy / "junk.skip").exists()
    assert os.stat(copy / "edit.txt").st_uid == 1234

    time.sleep(1.1)
    (src / "edit.txt").write_text("version 2\n")
    r2 = engine.run_job("t")
    s2 = snapshots.get(jd, r2["snapshot"])
    copy2 = s2.files / str(src).lstrip("/")
    assert os.stat(copy / "keep.txt").st_ino == os.stat(copy2 / "keep.txt").st_ino  # shared
    assert (copy2 / "edit.txt").read_text() == "version 2\n"
    assert (copy / "edit.txt").read_text() == "version 1\n"

    time.sleep(1.1)
    r3 = engine.run_job("t")
    assert r1["snapshot"] in r3["pruned"]
    names = [s.name for s in snapshots.list_snapshots(jd)]
    assert names == [r3["snapshot"], r2["snapshot"]]
    assert os.readlink(jd / "latest") == f"snapshots/{r3['snapshot']}"

    # Restore preview against the live tree, then a real restore elsewhere.
    s2 = snapshots.get(jd, r2["snapshot"])
    (src / "edit.txt").write_text("version 3\n")
    preview = restore.restore(s2, [str(src)], dry_run=True)
    changed = {i["path"] for i in preview["changes"]["items"] if i["change"] == "changed"}
    assert str(src / "edit.txt") in changed
    out = tmp_path / "out"
    res = restore.restore(s2, [str(src / "edit.txt")], target=str(out))
    restored = out / str(src / "edit.txt").lstrip("/")
    assert restored.read_text() == "version 2\n"
    assert os.stat(restored).st_uid == 1234 and res["undo"] is None

    # In place, with an undo copy of the replaced file.
    res = restore.restore(s2, [str(src / "edit.txt")])
    assert (src / "edit.txt").read_text() == "version 2\n"
    undo = os.path.join(res["undo"], str(src / "edit.txt").lstrip("/"))
    assert open(undo).read() == "version 3\n"


def test_resume_and_history(setup):
    src, dest = setup
    engine.run_job("t")
    jd = jobdir(dest)
    partial = snapshots.in_progress(jd) / "files" / str(src).lstrip("/")
    partial.mkdir(parents=True)
    (partial / "stale-file").write_text("left over from a crash")
    time.sleep(1.1)
    r = engine.run_job("t")
    snap = snapshots.get(jd, r["snapshot"])
    assert snap.manifest["resumed"] is True
    assert not (snap.files / str(src).lstrip("/") / "stale-file").exists()  # --delete cleaned it
    h = status.history("t", limit=5)
    assert h[0]["snapshot"] == r["snapshot"] and h[0]["status"] == "success"


def test_pacman_debounce_is_not_recorded(setup):
    engine.run_job("t")
    before = len(status.history("t", limit=100))
    r = engine.run_job("t", "pacman")
    assert r["status"] == "skipped"
    assert len(status.history("t", limit=100)) == before


def test_missing_folder_is_skipped(setup, tmp_path):
    cfg = config.load()
    cfg.destinations["d"].path = str(tmp_path / "unplugged")
    config.save(cfg)
    r = engine.run_job("t")
    assert r["status"] == "skipped"
    assert engine.exit_code(r) == engine.EXIT_SKIPPED


def test_restore_guards(tmp_path):
    with pytest.raises(MikoshiError):
        restore._check_original_target("/usr/bin/ls", force=False)
    restore._check_original_target("/usr/bin/ls", force=True)
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    with pytest.raises(MikoshiError):
        restore._check_original_target(str(tmp_path / "link" / "file"), force=False)


def test_browse_cannot_escape(tmp_path):
    snap_dir = tmp_path / "2026-01-01_00-00-00"
    (snap_dir / "files" / "etc").mkdir(parents=True)
    (snap_dir / "files" / "out").symlink_to("/etc")
    snap = snapshots.load(snap_dir)
    assert [i["name"] for i in restore.browse(snap, "/")] == ["etc", "out"]
    with pytest.raises(MikoshiError):
        restore.browse(snap, "/out")
    # ".." can't climb out: absolute paths are normalized, so this is the top of the snapshot.
    assert restore.browse(snap, "/../../") == restore.browse(snap, "/")
    # Reaching through a symlink that points outside is refused.
    with pytest.raises(MikoshiError):
        restore.browse(snap, "/out/ssh")


def test_fake_super_round_trip(tmp_path):
    """Owners survive a destination that stores them in xattrs (NFS with root squash)."""
    from mikoshi.destinations import Capabilities
    src = tmp_path / "src"
    src.mkdir()
    f = src / "owned.txt"
    f.write_text("x")
    os.chown(f, 1234, 5678)
    os.chmod(f, 0o640)
    dest = tmp_path / "dest"
    dest.mkdir()
    flags = Capabilities(mode="fake-super").rsync_flags() + engine.fake_super_opts()
    rc, _, errors = engine.run_rsync(["rsync", *flags, "-r", f"{src}/", engine.remote(f"{dest}/")])
    assert rc == 0, errors
    stored = dest / "owned.txt"
    assert os.stat(stored).st_uid == 0          # real owner not applied on the backup side...
    assert any(n.startswith("user.rsync") for n in os.listxattr(stored))  # ...but recorded

    snap_dir = tmp_path / "2026-01-01_00-00-00"
    (snap_dir / "files").mkdir(parents=True)
    os.rename(dest, snap_dir / "files" / "data")
    snap = snapshots.load(snap_dir)
    snap.manifest = {"metadata_mode": "fake-super"}
    out = tmp_path / "out"
    restore.restore(snap, ["/data/owned.txt"], target=str(out))
    back = out / "data" / "owned.txt"
    st = os.stat(back)
    assert (st.st_uid, st.st_gid, st.st_mode & 0o777) == (1234, 5678, 0o640)


def test_full_job_in_fake_super_mode(setup, tmp_path, monkeypatch):
    """A whole job through the remote-shell transport: owners recorded, links shared, restore exact."""
    from mikoshi import destinations
    real_probe = destinations.probe

    def fake_probe(opened):
        cap = real_probe(opened)
        cap.mode = "fake-super"
        return cap
    monkeypatch.setattr(destinations, "probe", fake_probe)
    src, dest = setup
    r1 = engine.run_job("t")
    assert r1["status"] == "success", r1
    time.sleep(1.1)
    r2 = engine.run_job("t")
    jd = jobdir(dest)
    s1, s2 = snapshots.get(jd, r1["snapshot"]), snapshots.get(jd, r2["snapshot"])
    rel = str(src / "edit.txt").lstrip("/")
    assert s2.manifest["metadata_mode"] == "fake-super"
    assert os.stat(s2.files / rel).st_uid == 0
    assert os.stat(s1.files / rel).st_ino == os.stat(s2.files / rel).st_ino
    out = tmp_path / "out"
    restore.restore(s2, [str(src)], target=str(out))
    assert os.stat(out / rel).st_uid == 1234
