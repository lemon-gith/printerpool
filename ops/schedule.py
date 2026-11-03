"""When the next scheduled backup is due, in the site's time zone."""

from datetime import datetime, time, timedelta

from django.utils import timezone

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def next_run(now, frequency, at="03:30", weekday="sunday"):
    """The first run time strictly after `now`.

    hourly: every hour at the minute of `at` (e.g. "03:30" -> hh:30)
    daily:  every day at `at`
    weekly: every `weekday` at `at`
    Times are wall-clock times in the current time zone, so they stay put across daylight-saving changes.
    """
    tz = timezone.get_current_timezone()
    local = timezone.localtime(now, tz)
    hour, minute = (int(part) for part in at.split(":"))

    if frequency == "hourly":
        candidate = local.replace(minute=minute, second=0, microsecond=0)
        if candidate <= local:
            candidate += timedelta(hours=1)
        return candidate
    if frequency not in ("daily", "weekly"):
        raise ValueError(f"unknown backup frequency {frequency!r}")

    day = local.date()
    for _ in range(9):  # at most a week ahead, plus slack
        if frequency == "daily" or day.weekday() == WEEKDAYS.index(weekday):
            candidate = timezone.make_aware(datetime.combine(day, time(hour, minute)), tz)
            if candidate > local:
                return candidate
        day += timedelta(days=1)
    raise AssertionError("no run time found")  # unreachable
