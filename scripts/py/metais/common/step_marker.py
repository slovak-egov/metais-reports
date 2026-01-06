from __future__ import annotations

from pathlib import Path
from typing import Union

from metais.common.atomic_write import atomic_write_with


PathLike = Union[str, Path]


def done_marker(dir_path: PathLike, marker: str = ".done") -> Path:
    """
    Return the marker file path inside dir_path.
      - marker defaults to ".done"
      - marker can be ".pass0.done", ".pass1_5.done", etc.
    """
    return Path(dir_path) / marker


def is_done(dir_path: PathLike, marker: str = ".done") -> bool:
    """
    True if marker file exists.
    Accepts either:
      - is_done(root)                 -> checks root/.done
      - is_done(root, ".pass2.done")  -> checks root/.pass2.done
    """
    return done_marker(dir_path, marker).is_file()


def mark_done(dir_path: PathLike, marker: str = ".done", payload: str = "ok\n") -> None:
    """
    Atomically write the marker file.

    Accepts either:
      - mark_done(root)                              -> writes root/.done
      - mark_done(root, ".pass2.done", "pass=2\\n")  -> writes root/.pass2.done
    """
    dirp = Path(dir_path)
    dirp.mkdir(parents=True, exist_ok=True)
    p = done_marker(dirp, marker)

    def _w(f) -> None:
        f.write((payload if payload else "ok\n").encode("utf-8"))

    atomic_write_with(p, _w)