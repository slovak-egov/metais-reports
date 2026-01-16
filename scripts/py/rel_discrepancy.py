from __future__ import annotations

from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Union, Optional

from metais.common.project_root import find_project_root
from metais.common.json_utils import load_json_file

from metais.packed_reader.packed_reader import PackedReader


Pathish = Union[str, Path]

# ------------------ metadata loader ------------------

def get_meta_rels(meta_rel_dir: Pathish) -> dict[str, dict[str, Any]]:
    meta_rel_dir = Path(meta_rel_dir)

    res: dict[str, dict[str, Any]] = {}
    for filename in meta_rel_dir.glob("*.json"):
        rel_metadata = load_json_file(filename)
        reltype = rel_metadata.get("technicalName") or filename.stem

        sources = rel_metadata.get("sources") or []
        targets = rel_metadata.get("targets") or []

        def _tn(x):
            return x.get("technicalName") if isinstance(x, dict) else None

        res[reltype] = {
            "sources": [s for s in (_tn(x) for x in sources) if isinstance(s, str)],
            "targets": [t for t in (_tn(x) for x in targets) if isinstance(t, str)],
            "cardinality": {
                "source": rel_metadata.get("sourceCardinality") or {"min": 0, "max": None},
                "target": rel_metadata.get("targetCardinality") or {"min": 0, "max": None},
            },
        }

    return res


# ------------------ verification ------------------

@dataclass
class ViolSample:
    citype: str
    local_index: int
    count: int
    why: str          # "below_min" | "above_max"
    url: Optional[str]

def _dense_counter(n: int) -> array:
    # 4 bytes per entry (unsigned int)
    return array("I", [0]) * n

def _card_bounds(obj: Any) -> tuple[int, Optional[int]]:
    # expects {"min": ..., "max": ...} with max possibly None
    if not isinstance(obj, dict):
        return 0, None
    mn = obj.get("min", 0)
    mx = obj.get("max", None)
    mn_i = int(mn) if mn is not None else 0
    mx_i = int(mx) if mx is not None else None
    return mn_i, mx_i

def _unconstrained(bounds: tuple[int, Optional[int]]) -> bool:
    return bounds[0] == 0 and bounds[1] is None

def collect_edge_examples_for_samples(
    pr: PackedReader,
    *,
    reltype: str,
    out_samples: list[dict],   # from samples_above (source nodes)
    in_samples: list[dict],    # from samples_above (target nodes)
    max_edges_per_node: int = 3,
    valid_only: bool = True,
) -> tuple[dict[tuple[str,int], list[dict]], dict[tuple[str,int], list[dict]]]:
    """
    Returns:
      out_edges[(citype, local)] = [ {rel_url, nb_url, nb_citype, nb_uuid}, ... ]
      in_edges[(citype, local)]  = same (but for incoming)
    """

    want_out = {(s["citype"], int(s["local_index"])) for s in out_samples}
    want_in  = {(s["citype"], int(s["local_index"])) for s in in_samples}

    out_edges: dict[tuple[str,int], list[dict]] = {k: [] for k in want_out}
    in_edges:  dict[tuple[str,int], list[dict]] = {k: [] for k in want_in}

    base = pr.base_url.rstrip("/")

    def done():
        return all(len(v) >= max_edges_per_node for v in out_edges.values()) and \
               all(len(v) >= max_edges_per_node for v in in_edges.values())

    # One scan is enough: we can fill both out/in from the same stream
    for src_node, tgt_node, relid, rel_uuid in pr.iterate_relations(
        reltype=reltype,
        by="src",
        as_nodes=True,
        include_relid=True,
        include_rel_uuid=True,
        rel_uuid_format="str",
        valid_only=valid_only,
    ):
        s_key = (src_node.citype, int(src_node.local_index))
        t_key = (tgt_node.citype, int(tgt_node.local_index))

        if s_key in out_edges and len(out_edges[s_key]) < max_edges_per_node:
            rel_url = f"{base}/relation/{src_node.citype}/{src_node.uuid_str()}/{rel_uuid}"
            out_edges[s_key].append({
                "rel_url": rel_url,
                "neighbor_url": pr.entity_url(tgt_node),
                "neighbor_citype": tgt_node.citype,
                "neighbor_uuid": tgt_node.uuid_str(),
            })

        if t_key in in_edges and len(in_edges[t_key]) < max_edges_per_node:
            # relation URL is always rooted at source in the UI pattern you’re using
            rel_url = f"{base}/relation/{src_node.citype}/{src_node.uuid_str()}/{rel_uuid}"
            in_edges[t_key].append({
                "rel_url": rel_url,
                "neighbor_url": pr.entity_url(src_node),
                "neighbor_citype": src_node.citype,
                "neighbor_uuid": src_node.uuid_str(),
            })

        if done():
            break

    return out_edges, in_edges

def verify_relations(
    *,
    date: str,
    valid_only: bool = True,
    sample_per_bucket: int = 8,
    dense_limit: int = 2_500_000,  # if citype has <= this many locals, we store a dense array
) -> dict[str, Any]:
    dump_root = find_project_root(".") / "output" / date
    meta_rel_root = dump_root / "metadata" / "relations"
    meta = get_meta_rels(meta_rel_root)

    report: dict[str, Any] = {
        "date": date,
        "missing_in_metadata": [],
        "endpoint_mismatches": [],
        "cardinality_violations": [],
    }

    with PackedReader(date=date, open_relation_partitions_max=64) as pr:
        packed_reltypes = sorted([p.name for p in pr.rels_dir.iterdir() if p.is_dir()])

        # 1) coverage
        for rt in packed_reltypes:
            if rt not in meta:
                report["missing_in_metadata"].append(rt)

        # 2) + 3) endpoints + cardinalities per reltype (single pass)
        for rt in packed_reltypes:
            m = meta.get(rt)
            if m is None:
                report["missing_in_metadata"].append(rt)
                continue

            allowed_src = set(m.get("sources", []))
            allowed_tgt = set(m.get("targets", []))

            src_bounds = _card_bounds(m["cardinality"].get("source"))  # sources per target (in-degree)
            tgt_bounds = _card_bounds(m["cardinality"].get("target"))  # targets per source (out-degree)

            # counters
            out_deg: dict[str, Any] = {}
            in_deg: dict[str, Any] = {}
            src_n: dict[str, int] = {}
            tgt_n: dict[str, int] = {}

            def ensure_counter(side: str, citype: str) -> Any:
                lr = pr._get_local_resolver(citype)
                n = int(lr.local_count)

                if side == "out":
                    src_n[citype] = n
                    if citype in out_deg:
                        return out_deg[citype]
                else:
                    tgt_n[citype] = n
                    if citype in in_deg:
                        return in_deg[citype]

                ctr = _dense_counter(n) if n <= dense_limit else {}
                if side == "out":
                    out_deg[citype] = ctr
                else:
                    in_deg[citype] = ctr
                return ctr

            def inc(counter: Any, idx: int) -> None:
                if isinstance(counter, array):
                    counter[idx] += 1
                else:
                    counter[idx] = counter.get(idx, 0) + 1

            # collect actual endpoint pairs + a sample edge for each pair
            # key = (src_citype, tgt_citype)
            endpoint_sample: dict[tuple[str, str], dict[str, Any]] = {}

            # single streaming pass, with proper valid_only filtering inside PackedReader
            for src_node, tgt_node, relid, rel_uuid in pr.iterate_relations(
                reltype=rt,
                by="src",
                as_nodes=True,
                include_relid=True,
                include_rel_uuid=True,
                rel_uuid_format="str",
                valid_only=valid_only,
            ):
                s_ci = src_node.citype
                t_ci = tgt_node.citype
                s_li = int(src_node.local_index)
                t_li = int(tgt_node.local_index)
                rid  = int(relid)

                inc(ensure_counter("out", s_ci), s_li)
                inc(ensure_counter("in",  t_ci), t_li)

                key = (s_ci, t_ci)
                if key not in endpoint_sample:
                    # build URLs without any scans (we already have src UUID + rel UUID)
                    base = pr.base_url.rstrip("/")
                    rel_url = f"{base}/relation/{s_ci}/{src_node.uuid_str()}/{rel_uuid}"
                    endpoint_sample[key] = {
                        "relid": rid,
                        "rel_uuid": rel_uuid,
                        "src_url": pr.entity_url(src_node),
                        "tgt_url": pr.entity_url(tgt_node),
                        "rel_url": rel_url,
                    }

            # ---- endpoint mismatches (now based on *actual edges that survived valid_only*) ----
            for (src_ci, tgt_ci), sample in endpoint_sample.items():
                bad_src = bool(allowed_src) and src_ci not in allowed_src
                bad_tgt = bool(allowed_tgt) and tgt_ci not in allowed_tgt
                if not (bad_src or bad_tgt):
                    continue

                report["endpoint_mismatches"].append({
                    "reltype": rt,
                    "packed_src": src_ci,
                    "packed_tgt": tgt_ci,
                    "meta_sources": sorted(allowed_src),
                    "meta_targets": sorted(allowed_tgt),
                    "src_not_allowed": bad_src,
                    "tgt_not_allowed": bad_tgt,
                    "looks_swapped": (src_ci in allowed_tgt) and (tgt_ci in allowed_src),
                    "sample": sample,
                })

            # ---- cardinality evaluation (unchanged except bounds wiring) ----
            def eval_citype(*, citype: str, n: int, counter: Any, bounds: tuple[int, Optional[int]]):
                mn, mx = bounds
                below = 0
                above = 0

                observed_min = None
                observed_max = 0

                samples_below: list[ViolSample] = []
                samples_above: list[ViolSample] = []

                def get_count(i: int) -> int:
                    if isinstance(counter, array):
                        return int(counter[i])
                    return int(counter.get(i, 0))

                for i in range(n):
                    c = get_count(i)

                    # observed stats over *all nodes*
                    if observed_min is None or c < observed_min:
                        observed_min = c
                    if c > observed_max:
                        observed_max = c

                    if c < mn:
                        below += 1
                        if len(samples_below) < sample_per_bucket:
                            try:
                                node = pr.get_node_by_local(citype, i, include_attrs=False, include_meta=False)
                                url = pr.entity_url(node)
                            except Exception:
                                url = None
                            samples_below.append(ViolSample(citype, i, c, "below_min", url))
                        continue

                    if mx is not None and c > mx:
                        above += 1
                        if len(samples_above) < sample_per_bucket:
                            try:
                                node = pr.get_node_by_local(citype, i, include_attrs=False, include_meta=False)
                                url = pr.entity_url(node)
                            except Exception:
                                url = None
                            samples_above.append(ViolSample(citype, i, c, "above_max", url))

                if observed_min is None:
                    observed_min = 0

                return {
                    "citype": citype,
                    "n_nodes": n,
                    "claimed_min": mn,
                    "claimed_max": mx,
                    "observed_min": int(observed_min),
                    "observed_max": int(observed_max),
                    "below_min": int(below),
                    "above_max": int(above),
                    "samples_below": [s.__dict__ for s in samples_below],
                    "samples_above": [s.__dict__ for s in samples_above],
                }


            out_results = []
            if not _unconstrained(tgt_bounds):
                out_results = [eval_citype(citype=ci, n=src_n[ci], counter=ctr, bounds=tgt_bounds)
                            for ci, ctr in out_deg.items()]

            in_results = []
            if not _unconstrained(src_bounds):
                in_results = [eval_citype(citype=ci, n=tgt_n[ci], counter=ctr, bounds=src_bounds)
                            for ci, ctr in in_deg.items()]

            # ---- attach proof edges for above-max samples (only when needed) ----
            # Flatten "above" samples (dicts) across citypes
            out_above: list[dict] = []
            for r in out_results:
                out_above.extend(r.get("samples_above", []))

            in_above: list[dict] = []
            for r in in_results:
                in_above.extend(r.get("samples_above", []))

            if out_above or in_above:
                out_edges, in_edges = collect_edge_examples_for_samples(
                    pr,
                    reltype=rt,
                    out_samples=out_above,
                    in_samples=in_above,
                    max_edges_per_node=3,
                    valid_only=valid_only,
                )

                # Attach edges back into the nested structures
                for r in out_results:
                    for s in r.get("samples_above", []):
                        key = (s["citype"], int(s["local_index"]))
                        s["edges"] = out_edges.get(key, [])

                for r in in_results:
                    for s in r.get("samples_above", []):
                        key = (s["citype"], int(s["local_index"]))
                        s["edges"] = in_edges.get(key, [])

            totals = {
                "out_below_min": sum(x["below_min"] for x in out_results),
                "out_above_max": sum(x["above_max"] for x in out_results),
                "in_below_min":  sum(x["below_min"] for x in in_results),
                "in_above_max":  sum(x["above_max"] for x in in_results),
            }


            report["cardinality_violations"].append({
                "reltype": rt,
                "targetCardinality_targets_per_source": {"min": tgt_bounds[0], "max": tgt_bounds[1]},
                "sourceCardinality_sources_per_target": {"min": src_bounds[0], "max": src_bounds[1]},
                "totals": totals,
                "out_degree_by_source_citype": out_results,
                "in_degree_by_target_citype": in_results,
            })

    return report

def fmt_bounds(mn, mx):
    return f"min={mn}, max={'∞' if mx is None else mx}"

def print_reltype_cardinality(item: dict, *, max_examples: int = 2):
    rt = item["reltype"]

    # These names are unambiguous:
    targets_per_source = item["targetCardinality_targets_per_source"]      # metadata: targetCardinality
    sources_per_target = item["sourceCardinality_sources_per_target"]      # metadata: sourceCardinality

    print(f"{rt}")

    print(f"  targets per source (targetCardinality) claimed: {fmt_bounds(targets_per_source['min'], targets_per_source['max'])}")
    for r in item.get("out_degree_by_source_citype", []):
        if r["below_min"] or r["above_max"]:
            print(f"    {r['citype']}: observed[min={r['observed_min']}, max={r['observed_max']}] "
                  f"below_min={r['below_min']} above_max={r['above_max']}")

            for s in r["samples_below"][:max_examples]:
                print(f"      example below_min: count={s['count']} url={s['url']}")

            for s in r["samples_above"][:max_examples]:
                print(f"      example above_max: count={s['count']} url={s['url']}")
                for e in s.get("edges", [])[:3]:
                    print(f"        rel={e['rel_url']} -> nb={e['neighbor_url']}")

    print(f"  sources per target (sourceCardinality) claimed: {fmt_bounds(sources_per_target['min'], sources_per_target['max'])}")
    for r in item.get("in_degree_by_target_citype", []):
        if r["below_min"] or r["above_max"]:
            print(f"    {r['citype']}: observed[min={r['observed_min']}, max={r['observed_max']}] "
                  f"below_min={r['below_min']} above_max={r['above_max']}")

            for s in r["samples_below"][:max_examples]:
                print(f"      example below_min: count={s['count']} url={s['url']}")

            for s in r["samples_above"][:max_examples]:
                print(f"      example above_max: count={s['count']} url={s['url']}")
                for e in s.get("edges", [])[:3]:
                    print(f"        rel={e['rel_url']} -> nb={e['neighbor_url']}")

if __name__ == "__main__":
    rep = verify_relations(date="14-01-2026", valid_only=True)

    print("Missing in metadata:", len(rep["missing_in_metadata"]))
    print("Endpoint mismatches:", len(rep["endpoint_mismatches"]), "\n")
    for m in rep["endpoint_mismatches"]:
        packed_partition = f"{m['packed_src']}__{m['packed_tgt']}"
        print(
            f"- {m['reltype']}  packed={packed_partition}  "
            f"bad_src={m['src_not_allowed']} bad_tgt={m['tgt_not_allowed']} "
            f"swapped?={m['looks_swapped']}\n"
            f"  meta sources={m['meta_sources']}\n"
            f"  meta targets={m['meta_targets']}"
        )
        s = m.get("sample")
        if isinstance(s, dict):
            print(f"  sample rel_url={s.get('rel_url')}\n")

    bad_items = []
    for item in rep["cardinality_violations"]:
        t = item.get("totals", {})
        total_bad = int(t.get("out_below_min", 0)) + int(t.get("out_above_max", 0)) \
                  + int(t.get("in_below_min", 0))  + int(t.get("in_above_max", 0))
        if total_bad:
            bad_items.append((total_bad, item))

    bad_items.sort(key=lambda x: x[0], reverse=True)

    print("Reltypes with cardinality violations:", len(bad_items))
    print()

    # Detailed print for top N
    TOP_N = 10
    for _, item in bad_items[:TOP_N]:
        print_reltype_cardinality(item, max_examples=2)
        print()