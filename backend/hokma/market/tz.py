"""Time zones without a system time zone database (Windows does not ship one for `zoneinfo`)."""
from datetime import datetime, timedelta, timezone, tzinfo


class NewYork(tzinfo):
    """America/New_York with the current US daylight saving rule (second Sunday of March to first Sunday of November)."""

    def utcoffset(self, dt):
        return timedelta(hours=-4) if self._dst(dt) else timedelta(hours=-5)

    def dst(self, dt):
        return timedelta(hours=1) if self._dst(dt) else timedelta(0)

    def tzname(self, dt):
        return "EDT" if self._dst(dt) else "EST"

    @staticmethod
    def _nth_sunday(year: int, month: int, nth: int) -> int:
        first = datetime(year, month, 1)
        first_sunday = 1 + (6 - first.weekday()) % 7
        return first_sunday + 7 * (nth - 1)

    def _dst(self, dt) -> bool:
        # Local wall-clock comparison: DST starts 02:00 local on March's 2nd Sunday and ends 02:00 local on November's 1st.
        start = datetime(dt.year, 3, self._nth_sunday(dt.year, 3, 2), 2)
        end = datetime(dt.year, 11, self._nth_sunday(dt.year, 11, 1), 2)
        naive = dt.replace(tzinfo=None)
        return start <= naive < end

    def fromutc(self, dt):
        naive = dt.replace(tzinfo=None)
        start_utc = datetime(dt.year, 3, self._nth_sunday(dt.year, 3, 2), 7)  # 02:00 EST
        end_utc = datetime(dt.year, 11, self._nth_sunday(dt.year, 11, 1), 6)  # 02:00 EDT
        offset = timedelta(hours=-4) if start_utc <= naive < end_utc else timedelta(hours=-5)
        return (naive + offset).replace(tzinfo=self)


NEW_YORK = NewYork()


def zone(name: str, fixed_offset_seconds: int = 0) -> tzinfo:
    """The zone for an IANA name. New York is built in; others use `zoneinfo` when the system has it, else the
    fixed offset the source reported."""
    if name == "America/New_York":
        return NEW_YORK
    if name == "UTC":
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # missing database or unknown name
        return timezone(timedelta(seconds=fixed_offset_seconds))
