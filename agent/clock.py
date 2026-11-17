"""All times inside the agent are naive local times (default Asia/Kolkata)."""
from datetime import datetime
from zoneinfo import ZoneInfo


def now_local(tz="Asia/Kolkata") -> datetime:
    return datetime.now(ZoneInfo(tz)).replace(tzinfo=None)


def from_epoch(seconds, tz="Asia/Kolkata") -> datetime:
    return datetime.fromtimestamp(int(seconds), ZoneInfo(tz)).replace(tzinfo=None)
