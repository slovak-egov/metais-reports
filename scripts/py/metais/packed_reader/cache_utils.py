from functools import lru_cache
from typing import Callable, Optional, TypeVar

T = TypeVar("T")

def maybe_lru(cache_size: Optional[int], fn: Callable[..., T]) -> Callable[..., T]:
    if cache_size == 0:
        return fn
    return lru_cache(maxsize=cache_size)(fn)  # None => unbounded