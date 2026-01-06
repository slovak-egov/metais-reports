from __future__ import annotations
from pathlib import Path

def find_project_root(start: Path | None = None) -> Path:
    """
    Walk up from `start` until we find `.git`.
    Returns the first directory containing `.git`.
    Fallback: returns the original starting directory (resolved).
    Start being a file also works. It will just start from the directory that contains that file
    """
    if start is None:
        start = Path.cwd()

    start = start.expanduser().resolve()

    # If caller passed a file path, start from its parent directory
    if start.exists() and start.is_file():
        start = start.parent

    current = start
    while True:
        git = current / ".git"
        if git.exists():  # optionally: git.is_dir() or git.is_file()
            return current

        parent = current.parent
        if parent == current:
            return start
        current = parent