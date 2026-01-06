from __future__ import annotations
from datetime import datetime
from zoneinfo import ZoneInfo

def today_date(tz: str = "Europe/Bratislava") -> str:
    return datetime.now(ZoneInfo(tz)).strftime("%d-%m-%Y")