Converting ndjson to binaries

root = output/DATE/packed

----------------------------------
| ## Global dictionary of values |
----------------------------------

– lives in root/dict = dict_root
- dict.bin
  - variable-offset UTF-8 encoded values (json.dump())
  - values are concatenated, streamable via offsets
- dict.offsets.bin
  - U64 offsets inside dict.bin
  - size = (N_values + 1)

------------
| ## Uuids |
------------

- UUID format: 8-4-4-4-12 hex
  - 32 hex digits = 128 bits = 16 bytes
  - stored as raw binary U128
    - implementation: 2×U64 (hi, lo) or 16×U8
- sorting:
  - (uuid1 < uuid2) = (hi1 < hi2) || (hi1 == hi2 && lo1 < lo2)
  - (uuid1 == uuid2) = (hi1 == hi2 && lo1 == lo2)

--------------------------------------
| ## Global UUID resolver (REQUIRED) |
--------------------------------------

- lives in root/uuids = uuid_root
- purpose:
  - resolve (uuid) -> (citype index, local index, global node id)
  - enable relations with mixed endpoint types
  - enable queries when citype is not known

Files:
- uuids.bin
  - U128 × N_nodes
  - sorted by UUID
- meta.bin (parallel arrays or struct-of-arrays)
  - global_node_id: U32
  - citype_index:   U8 (index into citypes.json)
  - local_index:    U32 (index inside citype)
- citypes.json
  - indexed list of citypes: ["KS", "AS", "ISVS", ...]
  - index used everywhere else

------------
| ## NODES |
------------

-------------------------------------------------------------------------------
| original, raw dump is in output/DATE/nodes/pages                            |
| all entities together, sorted by "\$cmdb_createdAt" for deterministic order |
| packed machinery lives inside root/nodes = node_root                        |
-------------------------------------------------------------------------------

- each citype lives in node_root/<citype> = ci_dir

### Inside ci_dir:
- uuids.bin
  - U128 × N_citype_entities
  - UUIDs belonging to this citype
  - sorted by UUID
  - used for fast binary search when citype is known
- global_ids.bin
  - U32 × N_citype_entities
  - same order as uuids.bin
  - maps local index -> global node id
  - enables (citype + uuid) -> global id in O(log N), with N = number of entities of said citype

### Attributes (two possible layouts, chosen per citype)
- Grid layout:
  - attributes.bin
    - fixed-width rows
    - one row per entity (by local index)
    - INT32 × N_attributes
    - value = dict index
    - missing sentinel = -1

- Dense layout:
  - attributes.bin
    - packed list of (attribute index, value index)
    - only non-missing attributes stored
    - entry size:
      - attribute index: U16
      - value index:     U32
      - total:           6 bytes
  - attribute_offsets.bin
    - U32 offsets into attributes.bin
    - size = (N_entities + 1)

- attributes.json
  - attribute dictionary for this citype
  - ordered list:
    [
      {
        "technicalName": "...",
        "name": "...",
        "description": "...",
        "hasEnum": "ENUM_NAME | null"
      },
      ...
    ]
  - derived from DATE/metadata/nodes/<citype>.json

### Meta attributes (ALWAYS grid, ALWAYS separate)
- metaAttributes.json
  - deterministic list of meta attributes:
    ["owner", "state", "createdBy", "createdAt", "lastModifiedBy", "lastModifiedAt"]
  - same for all nodes and relations

- metaAttributes.bin
  - grid layout only
  - INT32 × 6 per entity
  - stored separately so normal attribute reads don’t touch it

- format.json
  - {
      "attributeLayout": "grid" | "dense",
      "attributeCount": N,
      "denseEntrySize": 6,
      "metaAttributeCount": 6
    }

----------------
| ## RELATIONS |
----------------

--------------------------------------------------------------------------------
| original, raw dump is in output/DATE/relations/pages                         |
| all relations together, sorted by "\$cmdb_createdAt" for deterministic order |
| packed machinery lives inside root/relations = rel_root                      |
--------------------------------------------------------------------------------

- each reltype lives in rel_root/<reltype> = rel_dir
- relations have their own UUIDs, but:
  - relation UUID lookup is not required
  - no uuids.bin needed for relations

### Attributes
- same grid / dense logic as nodes
- attributes.bin may be omitted if no attributes exist
- metaAttributes.bin always exists (same 6)

### Relation edges (GLOBAL IDs)
- src.tgt.bin
  - pairs: (source_global_id, target_global_id)
  - each entry: U32 + U32
  - sorted by source_global_id

- src.tgt.offsets.bin
  - U32 offsets per source_global_id
  - enables fast adjacency lookup

- tgt.src.bin
  - pairs: (target_global_id, source_global_id)
  - sorted by target_global_id

- tgt.src.offsets.bin
  - U32 offsets per target_global_id

- meta.json
  - {
      "attributeLayout": "grid" | "dense",
      "sourceTypes": ["PO", ...],
      "targetTypes": ["KS", ...]
    }

### Relation index helpers
- rels.json
  - {
      "bySource": {
        "PO": ["PO_je_gestor_KS", ...]
      },
      "byTarget": {
        "KS": ["PO_je_gestor_KS", ...]
      }
    }

------------
| ## SIZES |
------------

Core primitives:

- UUID (U128):            16 bytes
- dict value index:        4 bytes (U32)
- global node id:          4 bytes (U32)
- local index:             4 bytes (U32)
- citype index:            1 byte  (U8) - currently we have 103 citypes and 230 reltypes. Pushing it.
- reltype_index:           2 bytes (U16) - future proof
- offset entry:            8 bytes (U64)
- grid attribute cell:     4 bytes (INT32)
- dense attribute entry:   6 bytes (U16 + U32)

Key files (per million nodes, rough):

- global/uuids.bin:        16 MB
- global/meta.bin:         ~9 MB (packed arrays)
- <citype>/uuids.bin:      16 MB × share
- <citype>/global_ids.bin:  4 MB × share
- relations src.tgt.bin:    8 bytes × N_relations