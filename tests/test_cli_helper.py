import io
import json
import sys

from lifeboat import cli, config, helper, schedule


def test_parser_has_every_command():
    p = cli.build_parser()
    for cmd in ("status", "run", "restore", "prune", "config", "schedule", "helper", "hook"):
        args = {"status": [], "run": ["x"], "restore": ["x", "y", "/z"], "prune": ["x"], "config": ["show"],
                "schedule": [], "helper": [], "hook": ["pacman"]}[cmd]
        assert p.parse_args([cmd, *args]).fn


def test_status_json_without_jobs(capsys):
    config.save(config.Config())
    assert cli.main(["status", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["jobs"] == []


def test_timer_and_cron_rendering():
    cfg = config.parse(config.example())
    text = schedule.render_timer(cfg.jobs["daily"])
    assert "OnCalendar=*-*-* 02:30:00" in text and "Persistent=true" in text
    assert "Unit=lifeboat@daily.service" in text
    assert "30 2 * * *" in schedule.cron_lines(cfg)


def test_udev_rule_only_for_drive_jobs():
    cfg = config.parse(config.example())
    assert schedule.render_udev(cfg) == ""
    cfg.jobs["daily"].triggers.on_drive_connect = True
    rule = schedule.render_udev(cfg)
    assert 'ENV{ID_FS_UUID}=="PUT-FILESYSTEM-UUID-HERE"' in rule
    assert "lifeboat-plug@daily.service" in rule


def test_helper_rejects_unknown_methods(monkeypatch):
    monkeypatch.setattr("os.geteuid", lambda: 0)
    lines = ['{"id": 1, "method": "_expire"}', '{"id": 2, "method": "ping"}', "not json",
             '{"id": 4, "method": "browse", "params": {"job": "nope", "snapshot": "x"}}']
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n".join(lines) + "\n"))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    helper.serve()
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert replies[0]["result"]["ready"]
    assert "Unknown request" in replies[1]["error"]
    assert replies[2]["result"]["version"]
    assert "error" in replies[3]
    assert "No job named" in replies[4]["error"]


def test_root_squash_gets_a_clear_message(tmp_path, monkeypatch):
    """Error 13 from a NAS that squashes root must explain the fix, not show a bare errno."""
    from pathlib import Path
    from lifeboat import cli, system
    monkeypatch.setattr(system, "fstype_of", lambda p: "nfs4")
    monkeypatch.setattr(system, "disks_for_path", lambda p: set())
    real_mkdir = Path.mkdir

    def refuse(self, *a, **k):
        if "lifeboat" in self.parts:
            raise PermissionError(13, "Permission denied", str(self))
        return real_mkdir(self, *a, **k)
    monkeypatch.setattr(Path, "mkdir", refuse)
    dest = config.Destination(name="nas", type="folder", path=str(tmp_path))
    res = cli.check_destination(dest, "pc")
    assert not res["ok"]
    assert "root squash" in res["message"] and "Maproot" in res["message"]
    assert "Errno" not in res["message"]


def test_locked_drive_and_unlock(monkeypatch):
    import subprocess
    import pytest
    from lifeboat import destinations, system
    from lifeboat.util import LifeboatError
    luks = {"name": "sdb1", "path": "/dev/sdb1", "type": "part", "fstype": "crypto_LUKS",
            "uuid": "aaaa-1111", "pkname": "sdb", "mountpoints": []}
    monkeypatch.setattr(system, "lsblk", type("F", (), {"__call__": lambda s: [luks], "cache_clear": lambda s: None})())
    monkeypatch.setattr(destinations.os.path, "exists", lambda p: True)
    dest = config.Destination(name="usb", type="drive", uuid="aaaa-1111")
    with pytest.raises(destinations.Locked) as exc:
        with destinations.open_destination(dest, "pc"):
            pass
    assert exc.value.uuid == "aaaa-1111"

    calls = []

    def fake_run(cmd, check=True, input=None, timeout=None, **kw):
        calls.append((cmd, input))
        return subprocess.CompletedProcess(cmd, 2 if input == "wrong" else 0, "", "No key available")
    monkeypatch.setattr(destinations, "run", fake_run)
    with pytest.raises(LifeboatError, match="Wrong passphrase"):
        destinations.unlock_drive("aaaa-1111", "wrong")
    assert destinations.unlock_drive("aaaa-1111", "secret") == "unlocked"
    cmd, given = calls[-1]
    assert given == "secret" and "secret" not in cmd and "--key-file=-" in cmd
