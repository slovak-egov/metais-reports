from __future__ import annotations

import re
from datetime import date, datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from os import PathLike
from typing import Union

Pathish = Union[str, Path, PathLike[str]]

TZ = ZoneInfo("Europe/Bratislava")
_RX_DATE_DIR = re.compile(r"^(?P<dd>\d{2})-(?P<mm>\d{2})-(?P<yy>\d{4})$")

def today_date(tz: str = "Europe/Bratislava") -> str:
    return datetime.now(ZoneInfo(tz)).strftime("%d-%m-%Y")

def parse_ddmmyyyy(s: str) -> datetime:
    return datetime.strptime(s, "%d-%m-%Y")

def today_ddmmyyyy() -> str:
    return datetime.now(TZ).strftime("%d-%m-%Y")


def find_latest_dump(root: Pathish) -> str:
    """
    Scan `root` for subdirectories named DD-MM-YYYY and return the latest name as a string.

    Example:
        latest_dump_dir_name(project_root / "output") -> "17-01-2026"
    """
    root_p = Path(root)

    best_name: str | None = None
    best_date: date | None = None

    try:
        it = root_p.iterdir()
    except FileNotFoundError as e:
        raise FileNotFoundError(f"Directory not found: {root_p}") from e
    except NotADirectoryError as e:
        raise NotADirectoryError(f"Not a directory: {root_p}") from e

    for child in it:
        if not child.is_dir():
            continue

        m = _RX_DATE_DIR.match(child.name)
        if not m:
            continue

        dd = int(m.group("dd"))
        mm = int(m.group("mm"))
        yy = int(m.group("yy"))

        try:
            d = date(yy, mm, dd)
        except ValueError:
            # skip invalid dates like 32-13-2026
            continue

        if best_date is None or d > best_date:
            best_date = d
            best_name = child.name

    if best_name is None:
        raise RuntimeError(f"No DD-MM-YYYY subdirectories found under: {root_p}")

    return best_name