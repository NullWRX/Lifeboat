import time
from pathlib import Path

from lifeboat import retention
from lifeboat.config import Retention
from lifeboat.snapshots import Snapshot

DAY = 86400


def snaps_every(hours: float, days: int, now: float) -> list[Snapshot]:
    out = []
    t = now - days * DAY
    while t <= now:
        name = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(t))
        out.append(Snapshot(name=name, path=Path("/x") / name, time=t, status="success"))
        t += hours * 3600
    return out


def rules(**kw):
    base = dict(keep_last=0, keep_hourly=0, keep_daily=0, keep_weekly=0, keep_monthly=0, keep_yearly=0)
    base.update(kw)
    return Retention(**base)


NOW = time.mktime((2026, 11, 7, 12, 0, 0, 0, 0, -1))


def test_keep_last_only():
    snaps = snaps_every(6, 10, NOW)
    dec = retention.decide(snaps, rules(keep_last=5), NOW)
    assert len(dec.keep) == 5
    kept = sorted(dec.keep)
    assert kept == sorted(s.name for s in sorted(snaps, key=lambda s: s.time)[-5:])


def test_daily_keeps_one_per_day():
    snaps = snaps_every(6, 30, NOW)
    dec = retention.decide(snaps, rules(keep_daily=7), NOW)
    days = {n[:10] for n in dec.keep}
    assert len(dec.keep) == 7 and len(days) == 7
    # The kept one in each day is that day's newest.
    for name in dec.keep:
        same_day = [s for s in snaps if s.name[:10] == name[:10]]
        assert name == max(same_day, key=lambda s: s.time).name


def test_rules_combine():
    snaps = snaps_every(24, 400, NOW)
    dec = retention.decide(snaps, rules(keep_last=3, keep_daily=7, keep_weekly=4, keep_monthly=6, keep_yearly=2), NOW)
    # 7 daily (covers last 3), weekly adds ~3 older, monthly adds ~5 older, yearly adds 1 older year.
    assert 14 <= len(dec.keep) <= 18
    assert len(dec.keep) + len(dec.remove) == len(snaps)
    assert all(s.name not in dec.keep for s in dec.remove)


def test_pinned_and_newest_are_never_removed():
    snaps = snaps_every(24, 20, NOW)
    oldest = min(snaps, key=lambda s: s.time)
    oldest.pinned = True
    dec = retention.decide(snaps, rules(keep_last=1), NOW)
    assert oldest.name in dec.keep
    newest = max(snaps, key=lambda s: s.time)
    assert newest.name in dec.keep
    assert len(dec.keep) == 2


def test_incomplete_snapshots_are_left_alone():
    snaps = snaps_every(24, 5, NOW)
    snaps[0].status = "incomplete"
    dec = retention.decide(snaps, rules(keep_last=1), NOW)
    assert snaps[0].name not in dec.keep
    assert snaps[0] not in dec.remove


def test_keep_all_and_keep_within():
    snaps = snaps_every(24, 20, NOW)
    assert not retention.decide(snaps, rules(keep_last=1, keep_all=True), NOW).remove
    dec = retention.decide(snaps, rules(keep_within_days=5), NOW)
    assert len(dec.keep) == 6  # today plus 5 days back


def test_space_victims_order():
    snaps = snaps_every(24, 5, NOW)
    snaps[1].pinned = True
    victims = retention.space_victims(snaps)
    newest = max(snaps, key=lambda s: s.time)
    assert newest not in victims and snaps[1] not in victims
    assert victims == sorted(victims, key=lambda s: s.time)
