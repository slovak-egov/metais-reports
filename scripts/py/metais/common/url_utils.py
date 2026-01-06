from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Union
from urllib.parse import urlsplit, urlunsplit

PathLike = Union[str, Path]

def strip_query(url: Optional[PathLike]) -> str:
    """
    Return URL/path without query params or fragments.
    Safe for logging (won't leak tokens).
    Works with both URLs and filesystem paths.
    """
    if url is None:
        return "<none>"

    try:
        s = os.fspath(url)  # Path -> str, str stays str
    except TypeError:
        return "<none>"

    if not s:
        return "<none>"

    try:
        u = urlsplit(s)
        return urlunsplit((u.scheme, u.netloc, u.path, "", ""))
    except Exception:
        return s