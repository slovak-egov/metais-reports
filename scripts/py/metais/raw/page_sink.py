from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


@dataclass(slots=True)
class PageStats:
    offset: int = 0
    limit: int = 0
    received: int = 0
    seconds: float = 0.0


class PageSink(ABC):
    @abstractmethod
    def begin_page(self, offset: int, limit: int) -> None: ...

    @abstractmethod
    def write_item(self, obj: Any) -> None: ...

    @abstractmethod
    def end_page(self, stats: PageStats) -> None: ...


class NullSink(PageSink):
    def begin_page(self, offset: int, limit: int) -> None:
        return

    def write_item(self, obj: Any) -> None:
        return

    def end_page(self, stats: PageStats) -> None:
        return


class NdjsonSink(PageSink):
    """
    Single-file NDJSON sink (mostly for debugging).
    Appends JSON lines and flushes at end_page.
    """
    def __init__(self, out_path: Path) -> None:
        self._out_path = Path(out_path)
        self._out_path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self._out_path, "ab")  # bytes for consistent UTF-8 writing

    def close(self) -> None:
        if getattr(self, "_fh", None) is not None:
            try:
                self._fh.close()
            finally:
                self._fh = None  # type: ignore[assignment]

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def begin_page(self, offset: int, limit: int) -> None:
        return

    def write_item(self, obj: Any) -> None:
        # nlohmann::json::dump() is compact by default -> use separators for compact output
        line = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        self._fh.write(line.encode("utf-8"))
        self._fh.write(b"\n")

    def end_page(self, stats: PageStats) -> None:
        self._fh.flush()