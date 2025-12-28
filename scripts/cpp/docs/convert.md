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
    - implementation: 2×U64 (hi, lo)
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
  - citype_index:   U16 (index into citypes.json)
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
- citype index:            2 bytes (U16) - currently we have 103 citypes and 230 reltypes. Future proofing.
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



------------------------
| ## Conversion passes |
------------------------

The converter runs in **two raw-reading passes** plus an **offline finalize pass**.

Goals:
- deterministic indices (attributes, citypes, UUID ordering)
- restart-safe artifacts (atomic writes)
- pack nodes/relations in a form that supports fast lookup and later densification

--------------------------------
| Pass 0: Bootstrap (no reads) |
--------------------------------

Create the packed root structure and write deterministic metadata stubs.

- Ensure directories exist:
  - root/
  - root/dict/
  - root/uuids/
  - root/nodes/
  - root/relations/

- Write metaAttributes.json (same content for nodes and relations):
  - root/nodes/metaAttributes.json
  - root/relations/metaAttributes.json
  - content:
    ["owner","state","createdBy","createdAt","lastModifiedBy","lastModifiedAt"]

All writes are atomic: write `*.tmp` then rename.

---------------------------------------------
| Pass 1: Prepass / Discovery (raw read #1) |
---------------------------------------------

Stream raw NDJSON pages and collect global and per-type information.
No binary packed output is produced in this pass (except optional incremental census).

Inputs:
- output/DATE/nodes/pages/*.ndjson
- output/DATE/relations/pages/*.ndjson

Collected in-memory (minimum required):
- Observed citypes (nodes):
  - set(citype)
- Observed reltypes (relations):
  - set(reltype)
- Observed attribute technicalNames:
  - nodes:    citype -> set(attrTechnicalName)
  - relations: reltype -> set(attrTechnicalName)
- Observed values for the global dictionary:
  - values from:
    - attributes[*].value
    - metaAttributes[*] (values of the 6 keys)
  - values are canonicalized via canonical_value(...)
- Node UUID ownership:
  - citype -> vector<UUID>   (UUIDs belonging to this citype)
  - also optionally:
    - vector<(UUID, citype)> for easier global merge later

Optional crash-safe census (incremental):
- output/DATE/census/nodes/<citype>.attrs.json
- output/DATE/census/relations/<reltype>.attrs.json
Atomic writes. This is advisory; the authoritative pack schema is emitted in Pass 1.5.

Validation rules (soft):
- Missing attributes array is tolerated (counts as missing_attributes).
- Missing node UUID is tolerated but counted; such objects cannot be packed later.
- Bad UUID strings are tolerated but counted.

-----------------------------------------------------------
| Pass 1.5: Freeze schema + build resolvers (no raw read) |
-----------------------------------------------------------

This pass turns Pass-1 discovery into **deterministic indices** and writes
the metadata and UUID resolver files.

A) Citype indexing (deterministic)
- Source of truth:
  - Prefer metadata list if available (DATE/metadata/citypes_list.json)
  - Fallback: observed set from Pass 1
- Write:
  - root/uuids/citypes.json
    - ["KS", "AS", "ISVS", ...]
    - index position = citype_index (U16)

B) Per-type attribute indexing (deterministic)
For each citype and reltype:
- Take observed attribute technicalNames (Pass 1)
- Sort alphabetically (lexicographic, byte/UTF-8 order)
- Assign indices 0..(A-1) in that sorted order
- Write:
  - root/nodes/<citype>/attributes.json
  - root/relations/<reltype>/attributes.json
  - These are derived from DATE/metadata/*/<type>.json when possible,
    but must at least contain the ordered list of technicalName.
- Write:
  - root/nodes/<citype>/format.json
  - root/relations/<reltype>/format.json
  - During first implementation we default to grid:
    {
      "attributeLayout": "grid",
      "attributeCount": A,
      "denseEntrySize": 6,
      "metaAttributeCount": 6
    }

C) Global dictionary finalization
- Collect all unique canonical values
- Sort values deterministically (lexicographic)
- Assign dictIndex = rank in sorted list
- Write:
  - root/dict/dict.bin         (concatenated json.dump() bytes)
  - root/dict/dict.offsets.bin (U64 offsets; size = N+1)
  - (optional) root/dict/meta.json { "valueCount": N, ... }

D) Per-citype UUID sorting + local indexing
For each citype:
- Sort UUIDs by (hi, lo) as defined above
- Define local_index = rank in sorted list
- Write:
  - root/nodes/<citype>/uuids.bin (U128 × N_citype_entities)
- (global_ids.bin is written after global IDs exist; see section E)

E) Global UUID resolver (required)
- Construct the full set of nodes as tuples:
  (UUID, citype_index, local_index)
  from the per-citype sorted UUID lists.
- Sort globally by UUID
- Define:
  global_node_id = rank in global sorted UUID list (U32)
- Write:
  - root/uuids/uuids.bin (U128 × N_nodes, sorted)
  - root/uuids/resolver.bin (parallel arrays or struct-of-arrays):
    - row corresponds to global id
    - citype_index:   U16
    - local_index:    U32
    - total: 2 + 4 = 6 bytes/row

F) Per-citype global id mapping
For each citype, using the globally assigned IDs:
- Write:
  - root/nodes/<citype>/global_ids.bin
    - U32 × N_citype_entities
    - aligned with root/nodes/<citype>/uuids.bin
    - i.e., global_ids[local_index] = global_node_id

All writes are atomic.

-------------------------------------------------
| Pass 2: Pack to GRID (raw read #2, streaming) |
-------------------------------------------------

In Pass 2 we stream raw NDJSON again and write packed binaries in **grid**
format for both attributes and metaAttributes.

Rationale:
- Grid is fixed-width per entity and supports easy random-write by local index.
- Sorting/densification decisions can be made later (offline).
- Dense requires variable-length packing; defer until after ordering is finalized.

A) Nodes (per citype)
For each citype:
- Preallocate (fill with sentinel -1):
  - root/nodes/<citype>/attributes.bin
    - size = N_citype_entities × A_citype × 4 bytes
    - cell type = INT32 dictIndex (or -1 if missing)
  - root/nodes/<citype>/metaAttributes.bin
    - size = N_citype_entities × 6 × 4 bytes
    - cell type = INT32 dictIndex (or -1 if missing)

For each raw node object:
- parse citype and uuid
- resolve local_index:
  - binary search uuid in root/nodes/<citype>/uuids.bin
- build a row:
  - attributes row:
    - initialize all A cells = -1
    - for each attribute in raw attributes[]:
      - attrIndex = lookup technicalName -> index (from attributes.json)
      - dictIndex = lookup value -> dict index (global dict)
      - set row[attrIndex] = dictIndex
  - meta row:
    - for each meta key in the fixed list:
      - dictIndex = lookup meta value -> dict index
      - store in fixed column order
- write rows at offsets:
  - attributes.bin seek to (local_index * A * 4) and overwrite row
  - metaAttributes.bin seek to (local_index * 6 * 4) and overwrite row

Notes:
- If an attribute technicalName is not in attributes.json:
  - default behavior: warn and ignore or append to "unknown" counters
  - strict mode may error (configurable)
- If uuid is missing/bad or not found in uuids.bin:
  - count and skip object (cannot pack)

B) Relations (per reltype)
For each reltype:
- Determine N_reltype relations encountered (may require a counting pass or
  incremental grow strategy; implementation may use append-only temp then finalize).
- Pack attributes + meta in grid layout like nodes, keyed by relation local index
  (encounter order). Relation UUID lookup is not required.

In parallel, collect edges for adjacency:
- For each relation:
  - resolve startUuid/endUuid to global_node_id using root/uuids/uuids.bin (binary search)
  - append to a temp edges file:
    - root/relations/<reltype>/tmp.edges.bin  (U32 src, U32 tgt) in encounter order

------------------------------------------------
| Pass 3: Finalize (no raw read; sort/rewrite) |
------------------------------------------------

This pass performs offline sorting and optional format conversion.

A) Relation edge adjacency (required)
For each reltype:
- Read tmp.edges.bin (pairs src,tgt)
- Produce:
  - src.tgt.bin
    - sort by (src_global_id, tgt_global_id)
  - src.tgt.offsets.bin
    - offsets per src_global_id for adjacency lookup
  - tgt.src.bin
    - sort by (tgt_global_id, src_global_id)
  - tgt.src.offsets.bin
    - offsets per tgt_global_id
- Write reltype meta.json with:
  - attributeLayout ("grid" initially; updated if densified)
  - sourceTypes / targetTypes (from observed endpoints)

B) Grid vs Dense decision (optional per type)
For each citype / reltype, compute attribute storage sizes:

Let:
- N = number of entities of the type
- A = attributeCount
- M = total number of non-missing attribute cells (unique attrs per entity)

Grid bytes:
- grid_bytes = N * A * 4

Dense bytes:
- dense_bytes = 6*M + 4*(N + 1)
  - 6 bytes per (attrIndex U16 + dictIndex U32)
  - 4 bytes per entity offset entry in attribute_offsets.bin

Decision:
- if dense_bytes < grid_bytes:
  - convert attributes.bin grid -> dense:
    - write attributes.bin (pairs) and attribute_offsets.bin in local-index order
    - update format.json attributeLayout = "dense"
- else keep grid:
  - update format.json attributeLayout = "grid"
MetaAttributes remain grid always.

C) Summary / invariants
- Packed schema indices (citype_index, attrIndex, dictIndex) are deterministic.
- UUID resolver enables:
  - uuid -> (citype_index, local_index, global_node_id)
  - (citype + uuid) -> global_node_id via per-citype uuids.bin + global_ids.bin
- Relations are stored as global-id edges with adjacency offsets.
- Packed outputs are restart-safe: atomic writes, no partial finalized files.