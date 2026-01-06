from __future__ import annotations

import mmap
from typing import Optional, Tuple, Union

from .binary_io import UUID_U128_BE, UUID_BYTES, Uuid128, UuidLike, normalize_uuid

MMapLike = Union[mmap.mmap, memoryview, bytes, bytearray]


def uuid_at(mm: MMapLike, idx: int) -> Tuple[int, int]:
    """
    idx-th UUID row (hi, lo) from uuids.bin memory map.
    """
    hi, lo = UUID_U128_BE.unpack_from(mm, idx * UUID_BYTES)
    return int(hi), int(lo)


def lower_bound_uuid(mm: MMapLike, target_hi: int, target_lo: int, n: int) -> int:
    """
    First index i in [0,n] such that uuid[i] >= target (lex on (hi,lo)).
    uuids.bin must be sorted by (hi, lo).
    """
    lo = 0
    hi = n
    unpack_from = UUID_U128_BE.unpack_from
    stride = UUID_BYTES

    while lo < hi:
        mid = (lo + hi) // 2
        u_hi, u_lo = unpack_from(mm, mid * stride)
        # compare lexicographically
        if (u_hi < target_hi) or (u_hi == target_hi and u_lo < target_lo):
            lo = mid + 1
        else:
            hi = mid
    return lo


def find_uuid_index(mm: MMapLike, target_hi: int, target_lo: int, n: int) -> Optional[int]:
    """
    Return index of (target_hi,target_lo) in uuids.bin, or None.
    """
    i = lower_bound_uuid(mm, target_hi, target_lo, n)
    if i >= n:
        return None
    u_hi, u_lo = uuid_at(mm, i)
    if u_hi == target_hi and u_lo == target_lo:
        return i
    return None


def find_uuid_index_u(mm: MMapLike, u: UuidLike, n: int) -> Optional[int]:
    """
    Convenience: accepts UUID string/UUID object/(hi,lo)/Uuid128.
    """
    uu = normalize_uuid(u)
    return find_uuid_index(mm, uu.hi, uu.lo, n)