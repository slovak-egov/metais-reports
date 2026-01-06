from metais.common.binary_io import (
    U16_LE, U32_LE, U64_LE, I32_LE,
    PAIR, EDGE_PAIR,
    EDGE_REC_BYTES, RELID_BYTES,
    UUID_U128_BE, UUID_BYTES,
    RESOLVER_ROW, RESOLVER_ROW_BYTES,
    MISSING_I32,
    BUF,

    Uuid128, UuidLike, normalize_uuid,
)
from metais.common.packed_spec import META_COLS