# Architecture Summary — MetaIS C++ Fetch Pipeline

This document defines the **authoritative behavior, assumptions, and invariants** of the MetaIS C++ fetch pipeline.
It is intended for both human contributors and automated tools (e.g. Codex) and must be kept in sync with code changes.

---

## Purpose

Fetch, persist, and prepare MetaIS data for downstream processing in a **restartable, deterministic, and scalable** way.

The pipeline focuses on **correct data acquisition and durable storage**, not interpretation or normalization.

---

## Pipeline Stages

The pipeline consists of three **independent, idempotent** stages:

1. **Enums**
2. **Metadata** (citypes, reltypes)
3. **Raw data** (nodes, relations)

Each stage:
- writes to a **date-scoped output directory**
- writes a `.done` marker only after successful completion
- can be rerun independently without corrupting outputs

---

## Fetch Modes

Raw data supports two execution modes:

- **Serial adaptive**
  - Adaptive paging (window size shrinks on failures)
- **Parallel fixed**
  - Workers claim fixed offsets via a shared state directory

**Invariant:** No two workers may write the same page offset.

---

## Core Design Goals

- Safe restarts after crashes or partial failures
- Deterministic output layout and filenames
- Byte-for-byte identical output across modes (for the same paging)
- Minimal coupling between:
  - HTTP fetching
  - paging strategy
  - output writing

---

## Output Contract

### Directory layout
- Root: `output/<date>/...`

### Raw shards
- Nodes: `nodes/pages/nodes.<offset:09>.ndjson`
- Relations: `relations/pages/rels.<offset:09>.ndjson`
- Temporary file: `<final>.tmp` → finalized via atomic rename

### NDJSON format
- UTF-8 text
- One JSON value per line (`obj.dump() + "\n"`)
- No surrounding arrays
- No pretty-printing or trailing spaces

---

## .done Markers

- A stage is complete if its root contains `.done`
- Sub-stage markers may exist (`enums/.done`, `metadata/.done`, etc.)
- `.done` is written **only after all expected outputs exist**
- `.tmp` files do not imply completion and may be overwritten
- Existing `.done` causes the stage to be skipped (idempotency)

---

## Observed Attribute Census

During raw fetch:
- Track **actually observed** attribute `technicalName`s per type
- Persist incrementally for crash-safe resume:
  - `output/<date>/census/nodes/<citype>.attrs.json`
  - `output/<date>/census/relations/<reltype>.attrs.json`
- Writes are atomic (`.tmp` → rename)
- Warn if observed attributes are missing from metadata
  - configurable to error in strict mode

---

## Global Dictionary of Values

All values appearing in attributes and metaAttributes are deduplicated into a
global, streamed dictionary:

Files:
- `dict.values.bin` — UTF-8 JSON-encoded values (concatenated)
- `dict.offsets.bin` — uint64 offsets (count + 1 entries)
- `dict.meta.json` — metadata (value count, etc.)

Lookup:
- Value `i` lives at:
  - `[offsets[i], offsets[i+1])` in `dict.values.bin`

This is a **storage primitive**, not semantic normalization.

---

## Authentication

- Token source: `METAIS_TOKEN` environment variable
- Never log or persist tokens
- Serial mode: refresh/re-prompt on 401/403
- Parallel mode: shared token refreshed under lock
- Abort after N consecutive auth failures (configurable)

---

## Error Handling Policy

### Retriable (with backoff)
- Transport errors, timeouts
- HTTP: 408, 429, 500, 502, 503, 504
- Actions:
  - exponential backoff
  - honor `Retry-After` for 429
  - reduce concurrency or page size on repetition

### Auth-retriable
- HTTP: 401, 403
- Refresh or re-prompt token, retry
- Abort after N failures

### Non-retriable (fail fast)
- HTTP: 400, 404, 405, 410, 415, 422, 451, 501, 505
- Exception:
  - per-item 404s may be skipped (configurable)

---

## Deterministic Server Failures (`$cmdb_*`)

If HTTP 500 response contains `$cmdb_` (e.g. `$cmdb_typeName is missing`):

- Treat as deterministic data corruption
- Enter **quarantine mode**:
  - binary-search window to isolate failing index
  - log skipped index
  - continue fetching remaining data
- Produce a run summary listing skipped items
- Best-effort behavior; does not mark stage incomplete

---

## Schema Drift Policy

- New attributes may appear at any time
- Invalidated attributes may still appear in raw data
- AttributeProfiles may introduce additional attributes
- Unknown fields are preserved verbatim

Raw objects are opaque except for:
- `uuid`
- `type`
- `attributes[]` shape
- `metaAttributes` shape

---

## Performance Assumptions

- Scale:
  - Nodes: up to ~2 million
  - Relations: up to ~10 million
- Memory:
  - streaming only; no full dataset in memory
- CPU:
  - JSON serialization cost acceptable
- Disk:
  - append-once, write-once artifacts

---

## Non-Goals

- No semantic interpretation of attributes
- No referential integrity enforcement
- No global ordering guarantees
- No enum validation beyond presence

---

## Invariants (Do Not Break)

- NDJSON format and shard naming are stable
- Serial and parallel modes produce identical output
- `.done` semantics define correctness and restartability
- Missing/invalidated attributes do not cause failures
- Tokens are never logged or written to disk

---
