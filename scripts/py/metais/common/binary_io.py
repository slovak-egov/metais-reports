from __future__ import annotations

import struct
import uuid as _uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterable, Iterator, Tuple, Union

from metais.common.atomic_write import atomic_write_with

##################
# Struct formats #
##################

U16_LE = struct.Struct("<H")
U32_LE = struct.Struct("<I")
U64_LE = struct.Struct("<Q")
I32_LE = struct.Struct("<i")

MISSING_I32 = -1
MISSING_I32_BYTES = I32_LE.pack(MISSING_I32)

# sparse entry: u16 attrIndex + u32 dictIndex  (6 bytes)
PAIR = struct.Struct("<HI")

# edges: (u32, u32)
EDGE_PAIR = struct.Struct("<II")

# UUID bytes on disk: standard UUID byte order == big-endian halves
UUID_U128_BE = struct.Struct(">QQ")
UUID_BYTES = UUID_U128_BE.size  # 16

# resolver row: (citype_index u16, local_index u32) = 6 bytes
RESOLVER_ROW = struct.Struct("<HI")
RESOLVER_ROW_BYTES = RESOLVER_ROW.size  # 6

EDGE_REC_BYTES = EDGE_PAIR.size   # 8
RELID_BYTES = U32_LE.size         # 4

# edge triple: (u32 src_local, u32 tgt_local, u32 relid)
EDGE_TRIPLE = struct.Struct("<III")
EDGE_TRIPLE_BYTES = EDGE_TRIPLE.size  # 12

BUF = 1024 * 1024  # 1MB


############################################
# UUID128 (shared across convert + reader) #
############################################

@dataclass(frozen=True, order=True, slots=True)
class Uuid128:
    """
    Canonical representation used across the project.

    Ordering is lexicographic on (hi, lo).
    On disk we store standard UUID bytes; packing as >QQ (hi,lo) matches that.
    """
    hi: int
    lo: int

    @property
    def bytes16(self) -> bytes:
        return self.to_bytes_be()

    @staticmethod
    def from_uuid(u: _uuid.UUID) -> "Uuid128":
        x = u.int
        hi = (x >> 64) & ((1 << 64) - 1)
        lo = x & ((1 << 64) - 1)
        return Uuid128(hi=hi, lo=lo)

    @staticmethod
    def from_string(s: str) -> "Uuid128":
        return Uuid128.from_uuid(_uuid.UUID(s))

    @staticmethod
    def from_bytes_be(b: bytes) -> "Uuid128":
        if len(b) != 16:
            raise ValueError(f"UUID bytes must be 16 bytes, got {len(b)}")
        hi, lo = UUID_U128_BE.unpack(b)
        return Uuid128(int(hi), int(lo))

    def to_uuid(self) -> _uuid.UUID:
        x = ((self.hi & ((1 << 64) - 1)) << 64) | (self.lo & ((1 << 64) - 1))
        return _uuid.UUID(int=x)

    def to_bytes_be(self) -> bytes:
        return UUID_U128_BE.pack(self.hi, self.lo)


UuidLike = Union[str, _uuid.UUID, Tuple[int, int], Uuid128]


def normalize_uuid(u: UuidLike) -> Uuid128:
    if isinstance(u, Uuid128):
        return u
    if isinstance(u, tuple):
        hi, lo = u
        if not isinstance(hi, int) or not isinstance(lo, int):
            raise TypeError("UUID tuple must be (int hi, int lo)")
        if hi < 0 or lo < 0:
            raise ValueError("UUID (hi, lo) must be non-negative")
        return Uuid128(hi=hi, lo=lo)
    if isinstance(u, str):
        return Uuid128.from_string(u)
    if isinstance(u, _uuid.UUID):
        return Uuid128.from_uuid(u)
    raise TypeError(f"Unsupported uuid type: {type(u).__name__}")


#######################################
# Primitive write helpers (file-like) #
#######################################

def write_u16_le(f: BinaryIO, v: int) -> None:
    f.write(U16_LE.pack(int(v) & 0xFFFF))

def write_u32_le(f: BinaryIO, v: int) -> None:
    f.write(U32_LE.pack(int(v) & 0xFFFFFFFF))

def write_u64_le(f: BinaryIO, v: int) -> None:
    f.write(U64_LE.pack(int(v) & 0xFFFFFFFFFFFFFFFF))

def write_i32_le(f: BinaryIO, v: int) -> None:
    f.write(I32_LE.pack(int(v)))

def write_uuid16(f: BinaryIO, u: UuidLike) -> None:
    uu = normalize_uuid(u)
    f.write(uu.to_bytes_be())


##########################################
# Convenience “write whole file” helpers #
##########################################

def write_u32le_file(path: Union[str, Path], values: Iterable[int]) -> None:
    path = Path(path)
    pack = U32_LE.pack

    def _w(f: BinaryIO) -> None:
        for v in values:
            f.write(pack(int(v) & 0xFFFFFFFF))

    atomic_write_with(path, _w)

def write_u64le_file(path: Union[str, Path], values: Iterable[int]) -> None:
    path = Path(path)
    pack = U64_LE.pack

    def _w(f: BinaryIO) -> None:
        for v in values:
            f.write(pack(int(v) & 0xFFFFFFFFFFFFFFFF))

    atomic_write_with(path, _w)

def write_uuid16_file(path: Union[str, Path], values: Iterable[UuidLike]) -> None:
    path = Path(path)

    def _w(f: BinaryIO) -> None:
        for u in values:
            write_uuid16(f, u)

    atomic_write_with(path, _w)


##########################
# Primitive read helpers #
##########################

def i32_sentinel_row(cols: int) -> bytes:
    return MISSING_I32_BYTES * int(cols)

def read_exact(f: BinaryIO, n: int) -> bytes:
    b = f.read(n)
    if len(b) != n:
        raise EOFError(f"expected {n} bytes, got {len(b)}")
    return b

def read_u16_le(f: BinaryIO) -> int:
    return int(U16_LE.unpack(read_exact(f, U16_LE.size))[0])

def read_u32_le(f: BinaryIO) -> int:
    return int(U32_LE.unpack(read_exact(f, U32_LE.size))[0])

def read_u64_le(f: BinaryIO) -> int:
    return int(U64_LE.unpack(read_exact(f, U64_LE.size))[0])

def read_uuid16(f: BinaryIO) -> Uuid128:
    return Uuid128.from_bytes_be(read_exact(f, UUID_BYTES))


def iter_edge_pairs(path: Union[str, Path]) -> Iterator[Tuple[int, int]]:
    """
    Stream (u32,u32) pairs from an edges file.
    """
    path = Path(path)
    rec = EDGE_PAIR.size
    unpack = EDGE_PAIR.unpack
    with open(path, "rb") as f:
        while True:
            b = f.read(rec)
            if not b:
                return
            if len(b) != rec:
                raise EOFError(f"truncated edge record in {path}")
            a, c = unpack(b)
            yield int(a), int(c)


#########
# Edges #
#########

def write_edgepairs_file(path: Union[str, Path], pairs: Iterable[Tuple[int, int]]) -> None:
    """
    Atomically write an edge-pairs file: repeated (u32,u32).
    """
    path = Path(path)
    pack = EDGE_PAIR.pack

    def _w(f: BinaryIO) -> None:
        for a, b in pairs:
            f.write(pack(int(a) & 0xFFFFFFFF, int(b) & 0xFFFFFFFF))

    atomic_write_with(path, _w)

def iter_edge_triples(path: Union[str, Path]) -> Iterator[Tuple[int, int, int]]:
    """
    Stream (u32,u32,u32) triples from a triples file.
    """
    path = Path(path)
    rec = EDGE_TRIPLE.size
    unpack = EDGE_TRIPLE.unpack
    with open(path, "rb") as f:
        while True:
            b = f.read(rec)
            if not b:
                return
            if len(b) != rec:
                raise EOFError(f"truncated edge triple in {path}")
            a, c, r = unpack(b)
            yield int(a), int(c), int(r)