"""When the next collection run starts. Pure, hence testable."""

import random
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


def next_slot(now: datetime, slots: tuple[time, ...], tz: ZoneInfo,
              jitter: timedelta, rng: random.Random) -> datetime:
    """The first slot strictly after `now`, in local time, plus a random delay.

    Slots are wall-clock times in `tz`, so a run set for 08:00 stays at 08:00
    across a daylight-saving change. A slot missed while the container was down
    is not caught up: computing forward from `now` is what guarantees that,
    since a burst of overdue runs on restart is exactly the robotic pattern the
    jitter exists to avoid. The jitter keeps fixed slots from hitting Instagram
    at the same second every day."""
    local_now = now.astimezone(tz)
    for day_offset in (0, 1):
        day = local_now.date() + timedelta(days=day_offset)
        for slot in slots:
            candidate = datetime.combine(day, slot, tzinfo=tz)
            # Against `now` in its own zone, not local_now: two datetimes sharing
            # a ZoneInfo compare as wall-clock times, ignoring the offset. In the
            # hour repeated when daylight saving ends, that put an already-past
            # slot in the future and ran it twice.
            if candidate.astimezone(timezone.utc) > now.astimezone(timezone.utc):
                return candidate + timedelta(seconds=rng.uniform(0, jitter.total_seconds()))
    raise ValueError("schedule has no slots")
