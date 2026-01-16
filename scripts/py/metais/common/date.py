from __future__ import annotations
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Bratislava")

def today_date(tz: str = "Europe/Bratislava") -> str:
    return datetime.now(ZoneInfo(tz)).strftime("%d-%m-%Y")

def parse_ddmmyyyy(s: str) -> datetime:
    return datetime.strptime(s, "%d-%m-%Y")

def today_ddmmyyyy() -> str:
    return datetime.now(TZ).strftime("%d-%m-%Y")

def find_latest_date_dir(output_root):
    candidates = []
    for p in output_root.iterdir():
        if not p.is_dir():
            continue
        try:
            dt = parse_ddmmyyyy(p.name)
        except ValueError:
            continue
        candidates.append((dt, p.name))
    return max(candidates)[1] if candidates else None