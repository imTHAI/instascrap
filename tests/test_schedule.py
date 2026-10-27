from datetime import datetime, time, timedelta
from random import Random
from zoneinfo import ZoneInfo

from instascrap.schedule import next_slot

PARIS = ZoneInfo("Europe/Paris")


def test_next_slot_same_day():
    now = datetime(2026, 10, 20, 9, 0, tzinfo=PARIS)
    assert next_slot(now, (time(8), time(14)), PARIS, timedelta(0), Random(1)) == \
        datetime(2026, 10, 20, 14, 0, tzinfo=PARIS)


def test_next_slot_rolls_to_tomorrow_without_catch_up():
    now = datetime(2026, 10, 20, 22, 0, tzinfo=PARIS)
    assert next_slot(now, (time(8), time(14)), PARIS, timedelta(0), Random(1)) == \
        datetime(2026, 10, 21, 8, 0, tzinfo=PARIS)


def test_next_slot_is_strictly_after_now():
    now = datetime(2026, 10, 20, 8, 0, tzinfo=PARIS)
    assert next_slot(now, (time(8),), PARIS, timedelta(0), Random(1)).day == 21


def test_next_slot_across_dst_change():
    now = datetime(2026, 10, 24, 22, 0, tzinfo=PARIS)   # winter time starts 25/10 03:00
    slot = next_slot(now, (time(8),), PARIS, timedelta(0), Random(1))
    assert slot.astimezone(PARIS).hour == 8
    assert slot.astimezone(PARIS).utcoffset() == timedelta(hours=1)


def test_next_slot_accepts_utc_now():
    now = datetime(2026, 10, 20, 7, 30, tzinfo=ZoneInfo("UTC"))   # 09:30 in Paris
    assert next_slot(now, (time(8), time(14)), PARIS, timedelta(0), Random(1)) == \
        datetime(2026, 10, 20, 14, 0, tzinfo=PARIS)


def test_jitter_stays_within_bounds():
    now = datetime(2026, 10, 20, 9, 0, tzinfo=PARIS)
    base = datetime(2026, 10, 20, 14, 0, tzinfo=PARIS)
    for seed in range(50):
        delta = next_slot(now, (time(14),), PARIS, timedelta(minutes=30), Random(seed)) - base
        assert timedelta(0) <= delta <= timedelta(minutes=30)


def test_repeated_hour_does_not_run_a_slot_twice():
    # 2026-10-25, Paris: 02:00-03:00 happens twice. A run started at the first
    # 02:30 (00:30Z) and finished at 02:15 CET (01:15Z) must not be scheduled
    # again for 02:30 CEST, which is 00:30Z and already past.
    now = datetime(2026, 10, 25, 1, 15, tzinfo=ZoneInfo("UTC"))
    slot = next_slot(now, (time(2, 30),), PARIS, timedelta(0), Random(1))
    assert slot > now
    assert slot.astimezone(PARIS).date().day == 26
