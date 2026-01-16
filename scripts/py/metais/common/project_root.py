from __future__ import annotations

from pathlib import Path
from os import PathLike
from typing import Union

Pathish = Union[str, Path, PathLike[str]]

def find_project_root(start: Pathish | None = None) -> Path:
    """
    Walk up from `start` until we find `.git`.
    Returns the first directory containing `.git`.
    Fallback: returns the original starting directory (resolved).
    Start being a file also works; it will start from the file's parent directory.
    """
    if start is None:
        start_path = Path.cwd()
    else:
        start_path = Path(start)

    # avoid exceptions if the path doesn't exist
    start_path = start_path.expanduser().resolve(strict=False)

    # If caller passed a file path, start from its parent directory
    if start_path.exists() and start_path.is_file():
        start_path = start_path.parent

    current = start_path
    while True:
        git = current / ".git"
        if git.exists():  # handles dir, file (worktree/submodule), etc.
            return current

        parent = current.parent
        if parent == current:
            return start_path
        current = parent