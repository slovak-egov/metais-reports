import struct

# attribute byte size
TYPE_CODE_BYTES = 2

# UUIDs
UUID_BYTES = 16

# Dense row layout
ATTR_INDEX_BYTES = 2
DICT_INDEX_BYTES = 4
ROW_OFFSET_BYTES = 8

 # Grid layout (sparse)
GRID_INT_BYTES = 4

# Relations
REL_INT_BYTES = 4
REL_PAIR_BYTES = 2 * REL_INT_BYTES

# Structs
INT32_LE = struct.Struct("<i")
U16_LE   = struct.Struct("<H")
U32_LE   = struct.Struct("<I")
U64_LE   = struct.Struct("<Q")

# placeholder for a missing attribute
MISSING_SENTINEL = -1

UINT_LE_STRUCTS = {
    1: struct.Struct("<B"),
    2: struct.Struct("<H"),
    4: struct.Struct("<I"),
}

def get_uint_le_struct(n_bytes: int) -> struct.Struct:
    try:
        return UINT_LE_STRUCTS[n_bytes]
    except KeyError:
        raise ValueError(f"Unsupported unsigned LE integer size: {n_bytes}")