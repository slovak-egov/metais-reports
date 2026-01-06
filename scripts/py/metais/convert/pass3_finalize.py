from __future__ import annotations

import json
import mmap
import os
import shutil
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Dict, Iterable, Iterator, List, Optional, Tuple, Union

from metais.common.json_utils import load_json_file
from metais.common.step_marker import is_done, mark_done
from metais.common.atomic_write import atomic_replace, atomic_write_json
from metais.common.packed_spec import META_COLS
from metais.common.binary_io import (
    BUF,
    EDGE_PAIR,
    EDGE_TRIPLE,
    EDGE_TRIPLE_BYTES,
    I32_LE,
    MISSING_I32,
    RESOLVER_ROW,
    RESOLVER_ROW_BYTES,
    write_edgepairs_file,
    write_u32le_file,
)

#########################################################
# Resolver loading (gid -> (citype_index, local_index)) #
#########################################################

@dataclass(slots=True)
class ResolverMaps:
    citype_of_gid: List[int]  # U16
    local_of_gid: List[int]   # U32


def _load_resolver_maps(resolver_bin: Path) -> ResolverMaps:
    if not resolver_bin.is_file():
        raise FileNotFoundError(resolver_bin)

    with resolver_bin.open("rb") as f:
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            sz = len(mm)
            if sz % RESOLVER_ROW_BYTES != 0:
                raise ValueError(f"resolver.bin size not multiple of {RESOLVER_ROW_BYTES}: {sz}")
            n = sz // RESOLVER_ROW_BYTES

            citype: List[int] = [0] * n
            local: List[int] = [0] * n

            # tight loop: unpack_from
            unpack_from = RESOLVER_ROW.unpack_from
            stride = RESOLVER_ROW_BYTES
            off = 0
            for i in range(n):
                ci, li = unpack_from(mm, off)
                citype[i] = int(ci)
                local[i] = int(li)
                off += stride

            return ResolverMaps(citype_of_gid=citype, local_of_gid=local)
        finally:
            mm.close()


def _load_citypes_list(citypes_json: Path) -> List[str]:
    j = load_json_file(citypes_json)
    if not isinstance(j, list) or not all(isinstance(x, str) for x in j):
        raise TypeError(f"citypes.json must be list[str]: {citypes_json}")
    return list(j)


def _load_attribute_layout(format_json: Path) -> str:
    # default is grid if missing
    if not format_json.is_file():
        return "grid"
    try:
        j = load_json_file(format_json)
        if isinstance(j, dict) and isinstance(j.get("attributeLayout"), str):
            return j["attributeLayout"]
    except Exception:
        pass
    return "grid"


####################################
# Pass 3A: finalize relation edges #
####################################

def _iter_edge_pairs_with_relid(path: Path) -> Iterator[Tuple[int, int, int]]:
    """
    Stream tmp.edges.bin as (src_gid, tgt_gid, relid) using chunked reads.
    """
    rec = EDGE_PAIR.size
    unpack_iter = EDGE_PAIR.iter_unpack

    relid = 0
    carry = b""
    with path.open("rb") as f:
        while True:
            chunk = f.read(BUF)
            if not chunk:
                break
            data = carry + chunk
            nrec = len(data) // rec
            rem = len(data) - nrec * rec
            if rem:
                carry = data[-rem:]
                data = data[:-rem]
            else:
                carry = b""

            for a, b in unpack_iter(data):
                yield int(a), int(b), relid
                relid += 1

        if carry:
            raise EOFError(f"truncated edge record in {path}")


def _clean_edges_root(edges_root: Path) -> None:
    """
    Remove stale <SRC>__<TGT> partitions before rewriting (prevents leftover junk on reruns).
    """
    if not edges_root.exists():
        edges_root.mkdir(parents=True, exist_ok=True)
        return
    for ent in edges_root.iterdir():
        # keep nothing; pass3 output is fully regenerable
        try:
            if ent.is_dir():
                shutil.rmtree(ent)
            else:
                ent.unlink()
        except Exception:
            # non-fatal; we will overwrite what we can
            pass


def _bucket_key(src_ci: int, tgt_ci: int) -> int:
    return ((int(src_ci) & 0xFFFF) << 16) | (int(tgt_ci) & 0xFFFF)

def _key_src_ci(k: int) -> int:
    return (int(k) >> 16) & 0xFFFF

def _key_tgt_ci(k: int) -> int:
    return int(k) & 0xFFFF


def finalize_relations(layout, *, do_finalize: bool = True, verbose: bool = True) -> None:
    """
    Pass 3A (required per spec): partition + sort tmp.edges.bin into per-(SRC,TGT) local-index adjacency files.

    Creates:
      root/relations/<reltype>/edges/<SRC>__<TGT>/{src.tgt.bin,tgt.src.bin,*.relid.bin,meta.json}
    Then deletes:
      root/relations/<reltype>/tmp.edges.bin   (only after successful finalization)
    """
    packed_root = Path(getattr(layout, "packed_root", Path(layout.date_root) / "packed"))
    rels_root = Path(getattr(layout, "rels_packed", packed_root / "relations"))
    uuids_root = Path(getattr(layout, "uuids_dir", packed_root / "uuids"))

    if not do_finalize:
        # You said your reader depends on this; so don’t silently “pretend ok”.
        # Still allow the CLI switch for debugging if you insist.
        if verbose:
            print("[pass3] WARNING: finalize_relations skipped (reader will not work).")
        return

    if not is_done(packed_root, ".pass2.rels.done"):
        raise RuntimeError("Pass 3 requires Pass 2 relations (.pass2.rels.done)")

    resolver_bin = uuids_root / "resolver.bin"
    citypes_json = uuids_root / "citypes.json"
    if not resolver_bin.is_file() or not citypes_json.is_file():
        raise RuntimeError("Pass 3 requires uuids/resolver.bin and uuids/citypes.json")

    if not rels_root.is_dir():
        if verbose:
            print(f"[pass3] no relations dir: {rels_root}")
        mark_done(packed_root, ".pass3.edges.done", "pass=3\nkind=edges\nnote=no-relations\n")
        return

    # Load resolver maps once
    if verbose:
        print("[pass3] loading resolver maps...")
    rm = _load_resolver_maps(resolver_bin)
    citype_of_gid = rm.citype_of_gid
    local_of_gid = rm.local_of_gid
    citypes = _load_citypes_list(citypes_json)
    if verbose:
        print(f"[pass3] resolver rows={len(citype_of_gid)} citypes={len(citypes)}")

    finalized = 0
    seen_tmp = 0

    for rel_dir_ent in rels_root.iterdir():
        if not rel_dir_ent.is_dir():
            continue

        rel_dir = rel_dir_ent
        reltype = rel_dir.name
        tmp_edges = rel_dir / "tmp.edges.bin"
        if not tmp_edges.is_file():
            continue

        seen_tmp += 1

        edges_root = rel_dir / "edges"
        _clean_edges_root(edges_root)

        # temp bucket area
        tmp_bucket_root = edges_root / ".pass3_tmp_buckets"
        if tmp_bucket_root.exists():
            shutil.rmtree(tmp_bucket_root, ignore_errors=True)
        tmp_bucket_root.mkdir(parents=True, exist_ok=True)

        attribute_layout = _load_attribute_layout(rel_dir / "format.json")

        if verbose:
            print(f"[pass3] reltype={reltype}: bucketing tmp.edges.bin -> {tmp_bucket_root}")

        # bucket streams by (src_ci,tgt_ci)
        bucket_out: Dict[int, BinaryIO] = {}

        def get_bucket_stream(key: int) -> BinaryIO:
            s_ci = _key_src_ci(key)
            t_ci = _key_tgt_ci(key)
            s_name = citypes[s_ci] if 0 <= s_ci < len(citypes) else f"CI_{s_ci}"
            t_name = citypes[t_ci] if 0 <= t_ci < len(citypes) else f"CI_{t_ci}"
            bucket_file = tmp_bucket_root / f"{s_name}__{t_name}.triples.bin"
            f = bucket_out.get(key)
            if f is not None:
                return f
            # append binary
            osf = bucket_file.open("ab")
            bucket_out[key] = osf
            return osf

        # Stream edges and write local-index triples into buckets
        n_edges = 0
        try:
            for src_gid, tgt_gid, relid in _iter_edge_pairs_with_relid(tmp_edges):
                if src_gid >= len(citype_of_gid) or tgt_gid >= len(citype_of_gid):
                    raise RuntimeError(
                        f"edge gid out of range at relid={relid} src={src_gid} tgt={tgt_gid}"
                    )
                src_ci = citype_of_gid[src_gid]
                tgt_ci = citype_of_gid[tgt_gid]
                key = _bucket_key(src_ci, tgt_ci)

                src_local = local_of_gid[src_gid]
                tgt_local = local_of_gid[tgt_gid]

                bout = get_bucket_stream(key)
                # IMPORTANT: locals, not gids
                bout.write(EDGE_TRIPLE.pack(
                    int(src_local) & 0xFFFFFFFF,
                    int(tgt_local) & 0xFFFFFFFF,
                    int(relid) & 0xFFFFFFFF,
                ))
                n_edges += 1
        finally:
            for f in bucket_out.values():
                try:
                    f.close()
                except Exception:
                    pass
            bucket_out.clear()

        # Finalize each bucket into edges/<SRC>__<TGT>/*
        if verbose:
            print(f"[pass3] reltype={reltype}: finalizing {n_edges} edges from buckets...")

        for bucket_path in tmp_bucket_root.iterdir():
            if not bucket_path.is_file():
                continue
            name = bucket_path.name
            if not name.endswith(".triples.bin"):
                continue

            pair_name = name[:-len(".triples.bin")]  # <SRC>__<TGT>
            out_dir = edges_root / pair_name
            out_dir.mkdir(parents=True, exist_ok=True)

            # load triples
            with bucket_path.open("rb") as bf:
                mm = mmap.mmap(bf.fileno(), 0, access=mmap.ACCESS_READ)
                try:
                    bsz = len(mm)
                    if bsz % EDGE_TRIPLE_BYTES != 0:
                        raise RuntimeError(f"bucket size not multiple of {EDGE_TRIPLE_BYTES}: {bucket_path}")
                    triples = [(int(a), int(b), int(r)) for (a, b, r) in EDGE_TRIPLE.iter_unpack(mm)]
                finally:
                    mm.close()

            # sort by (src_local, tgt_local)
            triples.sort(key=lambda t: (t[0], t[1]))

            st_pairs = [(s, t) for (s, t, _r) in triples]
            st_relid = [r for (_s, _t, r) in triples]

            write_edgepairs_file(out_dir / "src.tgt.bin", st_pairs)
            write_u32le_file(out_dir / "src.tgt.relid.bin", st_relid)

            # swapped: (tgt_local, src_local)
            swapped = [(t, s, r) for (s, t, r) in triples]
            swapped.sort(key=lambda t: (t[0], t[1]))

            ts_pairs = [(s, t) for (s, t, _r) in swapped]
            ts_relid = [r for (_s, _t, r) in swapped]

            write_edgepairs_file(out_dir / "tgt.src.bin", ts_pairs)
            write_u32le_file(out_dir / "tgt.src.relid.bin", ts_relid)

            # meta.json (partition)
            src_type = pair_name
            tgt_type = ""
            if "__" in pair_name:
                src_type, tgt_type = pair_name.split("__", 1)

            meta = {
                "reltype": reltype,
                "sourceType": src_type,
                "targetType": tgt_type,
                "relationCount": len(triples),
                "attributeLayout": attribute_layout,
            }
            atomic_write_json(out_dir / "meta.json", meta, ensure_ascii=False, indent=2)

        # cleanup temp buckets (non-fatal)
        shutil.rmtree(tmp_bucket_root, ignore_errors=True)

        # delete tmp.edges.bin ONLY after successful finalization
        tmp_edges.unlink()

        finalized += 1
        if verbose:
            print(f"[pass3] finalized reltype={reltype} edges={n_edges}")

    if verbose:
        print(f"[pass3] finalize_relations done: reltypes_with_tmp={seen_tmp} finalized={finalized}")

    mark_done(packed_root, ".pass3.edges.done", "pass=3\nkind=edges\n")


##########################################################
# Pass 3C: grid -> sparse optimization (attributes only) #
##########################################################

@dataclass(slots=True)
class FormatInfo:
    layout: str                  # "grid" | "sparse"
    attribute_count: int
    meta_attribute_count: int
    sparse_entry_bytes: Optional[int]


def _load_format(format_json: Path) -> FormatInfo:
    # default if missing
    if not format_json.is_file():
        return FormatInfo(layout="grid", attribute_count=0, meta_attribute_count=META_COLS, sparse_entry_bytes=None)

    j = load_json_file(format_json)
    if not isinstance(j, dict):
        return FormatInfo(layout="grid", attribute_count=0, meta_attribute_count=META_COLS, sparse_entry_bytes=None)

    layout = j.get("attributeLayout", "grid")
    if layout not in ("grid", "sparse"):
        layout = "grid"

    A = int(j.get("attributeCount", 0) or 0)
    M = int(j.get("metaAttributeCount", META_COLS) or META_COLS)

    seb = j.get("sparseEntryByteSize", None)
    if layout == "grid":
        if seb is not None:
            raise RuntimeError("format.json: grid layout must not define sparseEntryByteSize")
        return FormatInfo(layout="grid", attribute_count=A, meta_attribute_count=M, sparse_entry_bytes=None)

    # sparse
    if seb is None:
        raise RuntimeError("format.json: sparse layout missing sparseEntryByteSize")
    return FormatInfo(layout="sparse", attribute_count=A, meta_attribute_count=M, sparse_entry_bytes=int(seb))


def _count_rows_from_grid(attrs_bin: Path, A: int) -> int:
    if A <= 0:
        return 0
    sz = attrs_bin.stat().st_size
    row_bytes = A * 4
    if sz % row_bytes != 0:
        raise RuntimeError(f"attributes.bin size not multiple of row_bytes: {attrs_bin}")
    return sz // row_bytes


def _count_nonmissing_cells_grid(attrs_bin: Path, N: int, A: int) -> int:
    """
    Streaming count of non-missing cells (v != -1).
    """
    total_cells = N * A
    if total_cells == 0:
        return 0

    unpack_i32 = I32_LE.unpack_from
    cell_bytes = 4

    M = 0
    carry = b""

    with attrs_bin.open("rb") as f:
        while True:
            chunk = f.read(BUF)
            if not chunk:
                break
            data = carry + chunk
            rem = len(data) % cell_bytes
            if rem:
                carry = data[-rem:]
                data = data[:-rem]
            else:
                carry = b""

            # scan i32s
            for off in range(0, len(data), cell_bytes):
                v = int(unpack_i32(data, off)[0])
                if v != MISSING_I32:
                    M += 1

        if carry:
            raise EOFError(f"truncated i32 stream in {attrs_bin}")

    return M


def _grid_to_sparse_rewrite(
    attrs_bin_grid: Path,
    attrs_bin_tmp_sparse: Path,
    offsets_out: Path,
    N: int,
    A: int,
) -> None:
    """
    Write sparse attributes into attrs_bin_tmp_sparse and offsets_out (atomic),
    then caller replaces attributes.bin with attrs_bin_tmp_sparse.
    """
    attrs_bin_tmp_sparse.parent.mkdir(parents=True, exist_ok=True)

    offsets: List[int] = [0] * (N + 1)
    cur = 0

    with attrs_bin_grid.open("rb") as isf, attrs_bin_tmp_sparse.open("wb") as osf:
        row_bytes = A * 4
        pair_pack = struct.Struct("<HI").pack  # (u16 attrIndex, u32 dictIndex)

        for i in range(N):
            row = isf.read(row_bytes)
            if len(row) != row_bytes:
                raise EOFError(f"truncated row {i} in {attrs_bin_grid}")

            # scan cells
            for k in range(A):
                v = int(I32_LE.unpack_from(row, k * 4)[0])
                if v == MISSING_I32:
                    continue
                if v < 0:
                    # should never happen besides sentinel
                    continue

                osf.write(pair_pack(int(k) & 0xFFFF, int(v) & 0xFFFFFFFF))
                cur += META_COLS
                if cur > 0xFFFFFFFF:
                    raise RuntimeError(
                        f"sparse attributes exceeded 4GiB ({cur} bytes); need U64 offsets upgrade"
                    )

            offsets[i + 1] = cur

        osf.flush()

    # offsets are atomic via helper (writes its own .tmp then rename)
    write_u32le_file(offsets_out, offsets)


def _update_rel_edges_meta_layout(rel_dir: Path, new_layout: str) -> None:
    """
    Ensure edges/<SRC>__<TGT>/meta.json reflects the current rel_dir/format.json attributeLayout.
    Needed because main() runs finalize_relations() before optimize_attributes().
    """
    edges_root = rel_dir / "edges"
    if not edges_root.is_dir():
        return

    for part in edges_root.iterdir():
        if not part.is_dir():
            continue
        meta_path = part / "meta.json"
        if not meta_path.is_file():
            continue
        try:
            j = load_json_file(meta_path)
            if not isinstance(j, dict):
                continue
            if j.get("attributeLayout") == new_layout:
                continue
            j["attributeLayout"] = new_layout
            atomic_write_json(meta_path, j, ensure_ascii=False, indent=2)
        except Exception:
            # non-fatal; reader can still fall back to rel_dir/format.json if you want
            pass


def _maybe_convert_one_type(type_dir: Path, *, kind: str, verbose: bool) -> bool:
    """
    Returns True if conversion happened, else False.
    """
    format_json = type_dir / "format.json"
    attrs_bin = type_dir / "attributes.bin"
    offsets_bin = type_dir / "attribute_offsets.bin"

    if not format_json.is_file():
        return False
    if not attrs_bin.is_file():
        return False

    fmt = _load_format(format_json)

    # only grid -> sparse
    if fmt.layout != "grid":
        return False
    if fmt.attribute_count <= 0:
        # nothing to convert; also ensure stale offsets removed
        if offsets_bin.exists():
            try:
                offsets_bin.unlink()
            except Exception:
                pass
        return False

    A = fmt.attribute_count
    N = _count_rows_from_grid(attrs_bin, A)

    if N == 0:
        # nothing to do
        if offsets_bin.exists():
            try:
                offsets_bin.unlink()
            except Exception:
                pass
        return False

    # compute M + size compare
    M = _count_nonmissing_cells_grid(attrs_bin, N, A)

    grid_bytes = N * A * 4
    sparse_bytes = 6 * M + 4 * (N + 1)

    if sparse_bytes >= grid_bytes:
        # keep grid; delete stale offsets if any
        if offsets_bin.exists():
            try:
                offsets_bin.unlink()
            except Exception:
                pass
        return False

    if verbose:
        print(
            f"[pass3:sparse] converting {kind} dir={type_dir.name} "
            f"N={N} A={A} grid={grid_bytes} sparse={sparse_bytes} (M={M})"
        )

    # write sparse tmp then replace attributes.bin
    tmp_sparse = type_dir / "attributes.bin.sparse.tmp"
    _grid_to_sparse_rewrite(attrs_bin, tmp_sparse, offsets_bin, N, A)

    # atomic replace attributes.bin
    atomic_replace(tmp_sparse, attrs_bin)

    # update format.json
    j = load_json_file(format_json)
    if not isinstance(j, dict):
        j = {}
    j["attributeLayout"] = "sparse"
    j["attributeCount"] = A
    j["sparseEntryByteSize"] = 6
    if "metaAttributeCount" not in j:
        j["metaAttributeCount"] = fmt.meta_attribute_count

    atomic_write_json(format_json, j, ensure_ascii=False, indent=2)
    return True


def optimize_attributes(layout, *, do_optimize: bool = True, verbose: bool = True) -> None:
    """
    Pass 3C: optional grid->sparse conversion for attributes.bin (metaAttributes.bin untouched).
    Also fixes relation edges/*/meta.json attributeLayout to match rel_dir/format.json.
    """
    packed_root = Path(getattr(layout, "packed_root", Path(layout.date_root) / "packed"))
    nodes_root = Path(getattr(layout, "nodes_packed", packed_root / "nodes"))
    rels_root = Path(getattr(layout, "rels_packed", packed_root / "relations"))

    if not do_optimize:
        if verbose:
            print("[pass3:sparse] skipped")
        return

    if not is_done(packed_root, ".pass2.nodes.done") or not is_done(packed_root, ".pass2.rels.done"):
        raise RuntimeError("Pass 3 optimize requires Pass 2 done for nodes+rels")

    # Nodes: root/nodes/<citype>/
    if nodes_root.is_dir():
        for ent in nodes_root.iterdir():
            if not ent.is_dir():
                continue
            if not (ent / "format.json").is_file():
                continue
            _maybe_convert_one_type(ent, kind="node", verbose=verbose)

    # Relations: root/relations/<reltype>/
    if rels_root.is_dir():
        for ent in rels_root.iterdir():
            if not ent.is_dir():
                continue
            fmt_path = ent / "format.json"
            if not fmt_path.is_file():
                continue

            # possibly convert attributes.bin
            _maybe_convert_one_type(ent, kind="rel", verbose=verbose)

            # ensure edges meta.json attributeLayout matches final format.json
            try:
                new_layout = _load_attribute_layout(fmt_path)
                _update_rel_edges_meta_layout(ent, new_layout)
            except Exception:
                pass

    mark_done(packed_root, ".pass3.attrs.done", "pass=3\nkind=attrs\n")
    if verbose:
        print("[pass3:sparse] done")