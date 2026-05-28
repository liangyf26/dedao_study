from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


try:
    APP_TIMEZONE = ZoneInfo("Asia/Shanghai")
except ZoneInfoNotFoundError:
    APP_TIMEZONE = timezone(timedelta(hours=8), "Asia/Shanghai")


def now_local() -> datetime:
    return datetime.now(APP_TIMEZONE)
