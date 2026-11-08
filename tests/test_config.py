import tomllib
from pathlib import Path

import pytest

from mikoshi import config, tomlw

MAN5 = Path(__file__).resolve().parent.parent / "data" / "man" / "mikoshi.conf.5"


def test_example_config_is_valid():
    cfg = config.parse(config.example())
    assert config.validate(cfg) == []
    assert cfg.jobs["daily"].schedule.on_calendar() == "*-*-* 02:30:00"


def test_round_trip_through_writer():
    cfg = config.parse(config.example())
    cfg.jobs["daily"].exclude = ['weird "quoted" \\ path', "tab\there", "unicode ü ✓"]
    cfg.jobs["daily"].description = "line1\nline2"
    text = config.dumps(cfg)
    again = config.parse(text)
    assert config.to_dict(again) == config.to_dict(cfg)


def test_tomlw_matches_tomllib():
    data = {"a": 1, "b": True, "c": "x\u0001y", "d": [1, 2], "t": {"k-1": "v", "sub": {"z": 1.5}}, "q key": "v"}
    assert tomllib.loads(tomlw.dumps(data)) == data


def test_unknown_key_is_reported():
    with pytest.raises(config.ConfigError) as exc:
        config.parse('[jobs.a]\ndestination = "x"\nkeep_dayly = 3\n')
    assert "keep_dayly" in str(exc.value)


def test_wrong_types_are_reported():
    _, errors = config.from_dict({"jobs": {"a": {"modules": "etc", "enabled": "yes"}}})
    assert any("modules" in e for e in errors)
    assert any("enabled" in e for e in errors)


def _base():
    return config.parse(config.example())


@pytest.mark.parametrize("mutate, needle", [
    (lambda c: setattr(c.jobs["daily"], "modules", ["etc", "nope"]), "unknown module"),
    (lambda c: setattr(c.jobs["daily"], "destination", "missing"), "no destination named"),
    (lambda c: setattr(c.jobs["daily"].schedule, "time", "25:00"), "HH:MM"),
    (lambda c: setattr(c.jobs["daily"].schedule, "day", 31), "1 to 28"),
    (lambda c: setattr(c.jobs["daily"], "include", ["relative/path"]), "absolute"),
    (lambda c: c.jobs.update(BAD=c.jobs["daily"]), "name must be"),
    (lambda c: setattr(c.destinations["usb"], "uuid", "x"), "UUID"),
    (lambda c: setattr(c.jobs["daily"].triggers, "on_drive_connect", True) or
     setattr(c.destinations["usb"], "type", "folder") or setattr(c.destinations["usb"], "path", "/mnt/x"),
     "only works with a drive"),
])
def test_validation_errors(mutate, needle):
    cfg = _base()
    mutate(cfg)
    errors = config.validate(cfg)
    assert any(needle in e for e in errors), errors


def test_retention_needs_a_rule():
    cfg = _base()
    r = cfg.jobs["daily"].retention
    for f in ("keep_last", "keep_hourly", "keep_daily", "keep_weekly", "keep_monthly", "keep_yearly", "keep_within_days"):
        setattr(r, f, 0)
    assert any("keep rule" in e for e in config.validate(cfg))
    r.keep_all = True
    assert config.validate(cfg) == []


@pytest.mark.parametrize("mode, cal, cron", [
    ("manual", None, None),
    ("hourly", "*-*-* *:45:00", "45 * * * *"),
    ("daily", "*-*-* 03:45:00", "45 3 * * *"),
    ("weekly", "Wed *-*-* 03:45:00", "45 3 * * 3"),
    ("monthly", "*-*-07 03:45:00", "45 3 7 * *"),
])
def test_schedules(mode, cal, cron):
    s = config.Schedule(mode=mode, time="03:45", weekday="wed", day=7)
    assert s.on_calendar() == cal
    assert s.cron() == cron


def test_sunday_is_cron_zero():
    assert config.Schedule(mode="weekly", time="01:00", weekday="sun").cron() == "0 1 * * 0"


def test_man_page_example_is_valid():
    text = MAN5.read_text()
    block = text.split(".SH EXAMPLE", 1)[1].split(".nf", 1)[1].split(".fi", 1)[0]
    block = block.replace("\\-", "-")
    cfg = config.parse(block)
    assert config.validate(cfg) == []
    assert set(cfg.jobs) == {"system", "home"}
