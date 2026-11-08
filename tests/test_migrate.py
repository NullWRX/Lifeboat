"""Moving from Lifeboat (the earlier name) to Mikoshi."""

import hashlib
import json
import os

from conftest import needs_root, needs_rsync
from mikoshi import config, engine, identity, migrate, paths, snapshots, status

pytestmark = [needs_root, needs_rsync]


def test_lifeboat_install_is_carried_over(tmp_path, monkeypatch):
    old_etc, old_state, old_logs = tmp_path / "old-etc", tmp_path / "old-state", tmp_path / "old-logs"
    old_udev = tmp_path / "90-lifeboat.rules"
    monkeypatch.setattr(migrate, "LEGACY_CONFIG", old_etc)
    monkeypatch.setattr(migrate, "LEGACY_STATE", old_state)
    monkeypatch.setattr(migrate, "LEGACY_LOGS", old_logs)
    monkeypatch.setattr(migrate, "LEGACY_UDEV", old_udev)
    monkeypatch.setenv("MIKOSHI_MACHINE_ID", "same-machine")
    for f in (paths.CONFIG_FILE, paths.HISTORY_FILE):
        f.unlink(missing_ok=True)

    # A Lifeboat setup: config, history, a log, a timer, a drive rule...
    src = tmp_path / "src"
    src.mkdir()
    (src / "f").write_text("x")
    dest = tmp_path / "dest"
    old_etc.mkdir()
    (old_etc / "config.toml").write_text(f'''
[destinations.d]
type = "folder"
path = "{dest}"
same_disk_ok = true
[jobs.daily]
destination = "d"
include = ["{src}"]
[jobs.daily.schedule]
mode = "daily"
''')
    (old_etc / "secrets.toml").write_text('ntfy_token = "tok"\n')
    (old_state / "undo" / "2026-01-01").mkdir(parents=True)
    (old_state / "history.jsonl").write_text(json.dumps({"job": "daily", "action": "backup", "status": "success",
                                                         "finished": 1.0, "trigger": "timer"}) + "\n")
    (old_logs / "daily").mkdir(parents=True)
    (old_logs / "daily" / "x.log").write_text("log")
    paths.SYSTEMD_DIR.mkdir(parents=True, exist_ok=True)
    (paths.SYSTEMD_DIR / "lifeboat-daily.timer").write_text("# Managed by Lifeboat. ...\n[Timer]\n")
    old_udev.write_text("# Managed by Lifeboat.\n")
    # ...and backups at the destination, under the old folder name and old fingerprint.
    legacy_tag = hashlib.sha256(b"lifeboat:same-machine").hexdigest()[:24]
    host = dest / "lifeboat" / "pc"
    snap = host / "daily" / "snapshots" / "2026-01-01_00-00-00"
    (snap / "files" / str(src).lstrip("/")).mkdir(parents=True)
    (snap / "files" / str(src).lstrip("/") / "f").write_text("x")
    (snap / "manifest.json").write_text(json.dumps({"status": "success", "finished": 1.0, "modules": []}))
    (host / "host.json").write_text(json.dumps({"name": "pc", "machines": [legacy_tag]}))

    msgs = migrate.run_migration()
    assert msgs and not migrate.needed()
    assert config.load().jobs["daily"].schedule.mode == "daily"
    assert config.load_secrets().ntfy_token == "tok"
    assert oct(os.stat(paths.SECRETS_FILE).st_mode & 0o777) == "0o600"
    assert status.last_success("daily")["trigger"] == "timer"
    assert (paths.UNDO_DIR / "2026-01-01").is_dir() and (paths.LOG_DIR / "daily" / "x.log").exists()
    assert not (paths.SYSTEMD_DIR / "lifeboat-daily.timer").exists()
    assert (paths.SYSTEMD_DIR / "mikoshi-daily.timer").exists()
    assert not old_udev.exists() and not old_etc.exists()

    # The next backup renames the folder, recognises the machine, and continues the chain.
    cfg = config.load()
    cfg.settings.hostname = "pc"
    config.save(cfg)
    r = engine.run_job("daily")
    assert r["status"] == "success", r
    assert not (dest / "lifeboat").exists()
    jd = dest / "mikoshi" / "pc" / "daily"
    snaps = snapshots.list_snapshots(jd)
    assert len(snaps) == 2
    rel = str(src / "f").lstrip("/")
    assert os.stat(snaps[0].files / rel).st_ino == os.stat(snaps[1].files / rel).st_ino
    assert identity.machine_tag() in identity.read_host(jd.parent)["machines"]
