## Architecture (C++ fetch pipeline)

Fetches data from MetaIS (base: https://metais.slovensko.sk/) API

The pipeline consists of three independent stages:
1. Enums
2. Metadata (citypes, reltypes)
3. Raw data (nodes, relations)

Each stage:
- writes outputs into a date-scoped directory
- marks completion via `.done` files
- can be rerun independently

Raw data fetching supports:
- serial mode with adaptive paging
- parallel mode with fixed offsets

Design goals:
- safe restart after crashes
- deterministic output layout
- minimal coupling between fetching, paging, and output writing

## Inputs (MetaIS response structure)
- Enum list:
  - from endpoint https://metais.slovensko.sk/api/enums-repo/enums/list
  - top level: array [] of objects {}
  - each object:
    - "category": null/string,
    - "code": string - this is the name of the enum, e.g. "TYP_OSOBY" - we build a list of enums from these
    - "description": string
    - "name": string - this is the human description of the enum
    - "valid": bool
  - fetching enum list via open API, no authentication needed and should not be provided or required. Enums should force open mode regardless of METAIS_TOKEN being set.
- Individual enums:
  - from endpoint https://metais.slovensko.sk/api/enums-repo/enums/enum/valid/CODE
  - top level: array [] of objects {}
  - each object:
    - "code": string - this is the KEY for said enum, e.g. "c_typ_osoby.a1"
    - "description": string - this is the VALUE corresponding to the key, e.g. "ministerstvá"
    - "engDescription", "engValue", "id", "orderList", "qualifierKey", "qualifierName", "qualifierNameEng", "valid", "value" - not relevant for us
  - fetching enums via open API, no authentication needed and should not be provided or required
- Shape for metadata:
  - citypes list:
    - from endpoint https://metais.slovensko.sk/api/types-repo/citypes/list - open API, no authentication needed and should not be provided or required
    - top level: array [] of objects {}
    - each object:
      - "type": string - the citype, e.g. "KS", "AS", "ISVS", ...
      - "name": string, human description, e.g. "Koncová služba", "Aplikačná služba", ...
      - "valid": bool, whether the node/citype is valid or invalidated
  - individual citypes
    - from endpoint https://metais.slovensko.sk/api/types-repo/citypes/citype/TYPE - open API, no authentication needed and should not be provided or required.  Metadata should force open mode regardless of METAIS_TOKEN being set.
    - top level: object {}:
      - "attributes": object {}, describes all available attributes within that citype
        - "technicalName": string - this is how the attribute appears in raw data for fetched nodes, e.g. "Gen_Profil_nazov"
        - "name": human name for the attribute, e.g. "Názov aplikačnej služby"
        - "description": more elaborate description of the attribute
        - "attributeTypeEnum": string, optional - if the value of this attribute appears encoded and is decoded by an enum (fetched in the previous stage)
        - "valid": bool, whether this attribute is still used or was invalidated (but will be present in raw fetched nodes anyway)
      - "attributeProfiles": object {}:
        - "attributes": object {}, more attributes can be found here in the same format:
          - "technicalName": string - this is how the attribute appears in raw data for fetched nodes, e.g. "EA_Profil_typ_ISVS"
          - "name": human name for the attribute, e.g. "Typ informačného systému"
          - "description": more elaborate description of the attribute
          - "attributeTypeEnum": string, optional - if the value of this attribute appears encoded and is decoded by an enum (fetched in the previous stage)
          - "valid": bool, whether this attribute is still used or was invalidated (but will be present in raw fetched nodes anyway)
        - note: attributeProfiles contain several groups of additional attributes - these are not distinguished in the raw data dump and should be treated as on the same level
  - reltypes list:
    - from endpoint https://metais.slovensko.sk/api/types-repo/relationshiptypes/list - open API, no authentication needed and should not be provided or required.  Metadata should force open mode regardless of METAIS_TOKEN being set.
    - top level: array [] of objects {}
    - each object:
      - "type": string - the citype, e.g. "PO_je_gestor_KS", "Projekt_realizuje_AS", "Projekt_realizuje_ISVS", ...
      - "name": string, human description, e.g. "Povinná osoba je gestor koncovej služby", "Projekt realizuje aplikačnú službu", ...
      - "valid": bool, whether the relation type is valid or invalidated
  - individual reltypes
    - from endpoint https://metais.slovensko.sk/api/types-repo/relationshiptypes/relationshiptype/TYPE - open API, no authentication needed and should not be provided or required
    - top level: object {}:
      - "attributes": object {}, describes all available attributes within that reltype
        - "technicalName": string - this is how the attribute appears in raw data for fetched relations, e.g. "Gen_Profil_Rel_kod_metais"
        - "name": human name for the attribute, e.g. "Kód vzťahu MetaIS"
        - "description": more elaborate description of the attribute
        - "attributeTypeEnum": string, optional - if the value of this attribute appears encoded and is decoded by an enum (fetched in the previous stage)
        - "valid": bool, whether this attribute is still used or was invalidated (but will be present in raw fetched relations anyway)
      - "attributeProfiles": object {}:
        - "attributes": object {}, more attributes can be found here in the same format:
          - "technicalName": string - this is how the attribute appears in raw data for fetched relations, e.g. "EA_Profil_Rel_typ_vazby"
          - "name": human name for the attribute, e.g. "Typ väzby"
          - "description": more elaborate description of the attribute
          - "attributeTypeEnum": string, optional - if the value of this attribute appears encoded and is decoded by an enum (fetched in the previous stage)
          - "valid": bool, whether this attribute is still used or was invalidated (but will be present in raw fetched relations anyway)
        - note: attributeProfiles contain several groups of additional attributes - these are not distinguished in the raw data dump and should be treated as on the same level
- Common shape for nodes and relations:
  - "type": "RAW"
  - "result" or "results": array of objects
- Node object (expected fields):
  - type: <citype string> (e.g. "PO", "KS", "AS", "ISVS")
  - uuid: <uuid string>
  - attributes: [{name: string, value: any}, ...] - not all attributes of the same entity type are always required and present
  - metaAttributes: {"owner": string, "state": "DRAFT" | "INVALIDATED", "createdAt": string (date and time), "createdBy": string, "lastModifiedAt": string, lastModifiedBy: string} (always the same 6, should be all present)
  - as we are fetching, we should be keeping track of all OBSERVED attributes (actually present in the entities)
    - memory: keep a set() of seen attributes, updating on the go
    - disk: keep a list of seen attributes so far. In case of a crash we can load the list and continue where we left off
    - we should raise an alarm if we saw an attribute we did not see in the metadata list of attributes or attributes within attributeProfiles
- Relation object (expected fields):
  - "type": <reltype string> (.e.g "PO_je_gestor_KS") - if the type is missing on some relation within the requested window, the fetch will fail with http 500 and error message mentioning $cmdb_typeName is missing. There is one relation at some random index that is broken like this and when we get 500 + text containing "$cmdb_typeName", we should launch a binary search narrowing the window until we find the index responsible for the crash. Then we skip the index (fetch relations up to that index and then from index+1 onwards).
  - "uuid": string of the standard uuid format in groups of 8-4-4-4-12 characters, unique across all database objects and always present
  - "startUuid": uuid of the source entity in the relation
  - "endUuid": uuid of the target entity in the relation
  - attributes: [...]
  - metaAttributes: {...} (always the same 6)
  - as we are fetching, we should be keeping track of all OBSERVED attributes (actually present in the entities)
    - memory: keep a set() of seen attributes, updating on the go
    - disk: keep a list of seen attributes so far. In case of a crash we can load the list and continue where we left off
    - we should raise an alarm if we saw an attribute we did not see an the metadata list of attributes or attributes within attributeProfiles

## Global dictionary of values
- While we're fetching nodes and relations, we look at each value (inside attributes, metaAttributes, ...) and keep a global set() of the seen values. A lot of values repeat - names, numbers, emails...We also stream the list to the disk as a binary file with variable offsets - UTF8-encoded json objects. We also keep separate binary file recording the offsets. Example: value for attribute "Gen_Profil_nazov" in some entity is 2345. So we go to the global_dict_offsets.bin to the position 2345, grab the 32bit unsigned int (position), then from the position 2346 we grab the upper bound and in the global_dict.bin we look between the two positions to retrieve the actual value.

## Auth + Token
- Token source: METAIS_TOKEN env var (Bearer ...)
- Refresh:
  - both modes (serial_streaming/parallel_fixed): resolve in the beginning, ask if missing
  - serial mode: retry on 401/403
  - parallel mode: shared token with refresh under lock
- Authentication via client id and secret:
  - to be implemented upon getting the documentation

## Paging
- Modes:
  - serial adaptive: AdaptivePager + policy JSON
  - parallel_fixed: offsets claimed via state dir
- Invariants:
  - no two workers write the same offset

## Output contract
- Root: output/<date>/...
- Raw pages:
  - nodes/pages/nodes.<offset:09>.ndjson
  - relations/pages/rels.<offset:09>.ndjson
  - temp file: + ".tmp", atomic rename
- .done markers:
  - what they mean (stage completeness)
  - where they live
  - idempotency rules

## Downstream goals
- attribute census: collect unique attribute names per type
- deterministic ordering: assign indices for packing
- binary pack format: (brief contract + pointer to spec)

### Invariants (must not change without explicit decision)
- NDJSON output is line-oriented: one JSON object per line, no surrounding array.
- Shard filenames and directory layout are stable and deterministic.
- Serial and parallel modes must produce byte-for-byte compatible NDJSON output
  for the same page window.
- `.done` markers define stage completeness and enable safe restarts.
- Missing or invalidated attributes must not cause fetch failures.
- Token values must never be logged or written to disk.

### Non-Goals
- This pipeline does NOT normalize or interpret attribute semantics.
- This pipeline does NOT enforce referential integrity between nodes and relations.
- This pipeline does NOT guarantee ordering of entities across pages.
- This pipeline does NOT validate enum correctness beyond presence.

## Error Handling Philosophy

- Network failures:
  - Transient HTTP errors (5xx, timeouts) should be retried, except when the answer from the API contains something about "$cmdb_typeName" (or probably any other "$cmdb_" thing, something might be broken in the database.
  - Permanent errors (4xx other than auth) fail the stage.

- Auth errors:
  - 401/403 trigger token refresh or re-prompt. keep asking for token (if in the "token" authentication branch, the other one being client secret/id) until we get something other than 401/403

- Data errors:
  - Missing optional fields are tolerated.
  - Missing required identifiers (uuid, type) are fatal for the affected page.
  - Partial pages must not produce partial output shards.

- Restart behavior:
  - A failed page must not leave a finalized shard file.
  - `.tmp` files may remain and are safe to overwrite.
  - the run should be restartable from the last successful page

### HTTP status policy (non-200)

- Retriable (with backoff): 408, 429, 500, 502, 503, 504 + transport errors/timeouts.
  - For 429: honor Retry-After if present; also reduce concurrency if in parallel mode.
  - For repeated 5xx at same offset: shrink page/window size and retry.

- Auth-retriable: 401, 403
  - Refresh/re-prompt token; retry.
  - If repeats N times consecutively: abort (bad token or no access).

- Non-retriable (fail fast): 400, 404, 405, 410, 415, 422, 451, 501, 505
  - Exception: for per-item endpoints (enum/citype/reltype by key), 404 may be treated as “skip item and continue” (configurable).

### Deterministic server failures ($cmdb_*)

When HTTP 500 contains "$cmdb_" (e.g. "$cmdb_typeName is missing"):
- Treat as deterministic data corruption for that window.
- Enter "quarantine mode":
  - binary search within the current window to isolate the failing index
  - record the failing offset/index in a quarantine log
  - skip that single item and continue
- This is best-effort behavior and must produce a run summary listing skipped indices.

## Schema Evolution & Drift

- New attributes may appear at any time.
- Invalidated attributes may continue appearing in raw data.
- AttributeProfiles may introduce new attributes
- Unknown fields must be preserved verbatim in NDJSON output.

The pipeline treats raw objects as opaque containers except for:
- uuid
- type
- attributes[] shape
- metaAttributes shape

## Performance & Scale Assumptions

- Expected scale:
  - Nodes: up to 2 million
  - Relations: up to 10 millions
- Memory:
  - Pages must be streamed; entire datasets must not be held in memory.
- CPU:
  - Serialization cost is acceptable; compression is deferred.
- Disk:
  - NDJSON shards are append-once, write-once artifacts.

### Observed attribute census (during fetch)

For each type (citype / reltype):
- maintain an in-memory set of observed attribute technicalNames
- persist incrementally to disk so crashes can resume:
  - output/<date>/census/nodes/<citype>.attrs.json
  - output/<date>/census/relations/<reltype>.attrs.json
- raw pages must be atomic (write .tmp then rename), small json enums/metadata may be direct writes
- warn when observed attribute is missing from metadata (attributes + attributeProfiles)
  - severity: warning by default; configurable to error

## .done markers

- A stage is complete when its stage root contains a `.done` file.
- A stage may also create sub-step markers:
  - enums/.done
  - metadata/.done
  - nodes/.done
  - relations/.done
- `.done` must only be written after all expected outputs for that step exist.
- On startup, if `.done` exists, the step is skipped (idempotent).
- `.tmp` leftovers do not imply completion and may be overwritten.

### NDJSON formatting
- Each shard is UTF-8 text.
- One JSON value per line, written via `obj.dump()` followed by `\n`.
- No pretty-printing; no trailing spaces.

## Validation & Observability

- Validation:
  - Shard existence + `.done` markers define completeness.
  - No global checksum is computed at fetch time.
- Logging:
  - Log page offsets, counts, retries, and errors.
  - Never log full payloads or tokens.
- Determinism:
  - Given identical inputs and paging, output layout is deterministic.