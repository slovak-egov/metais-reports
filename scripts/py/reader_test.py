#!/usr/bin/env python3
from __future__ import annotations

import time

from metais.packed_reader.packed_reader import PackedReader
from metais.common.project_root import find_project_root

def needed_open_partitions(pr, citype: str, role: str = "either") -> int:
    reltypes = pr._reltypes_for_citype(citype, role)
    n = 0
    for rt in reltypes:
        for src, tgt, _path in pr._get_reltype_partitions(rt):
            if role in ("source", "either") and src == citype:
                n += 1
            elif role in ("target", "either") and tgt == citype:
                n += 1
    return n

def main() -> None:
    root = find_project_root()

    t0 = time.perf_counter()

    total_entities = 0
    total_neighbors = 0

    # Timing accumulators
    sum_entity_s = 0.0          # time spent per entity iteration (body)
    sum_getattr_s = 0.0         # time spent in get_attr_value
    sum_neighbors_loop_s = 0.0  # time spent iterating neighbors

    with PackedReader(
        root / "output/06-01-2026/packed",
        dict_cache_size=16384,
        attr_cache_size=1024,
        resolver_cache_size=1024,
        open_relation_partitions_max=None,
    ) as pr:
        for ci in pr.traverse_all_citypes(include_attrs=True, include_meta=False):
            t_ent0 = time.perf_counter()

            gid = ci.gid
            citype = ci.citype

            t_a0 = time.perf_counter()
            name = pr.get_attr_value(ci, "Gen_Profil_nazov")
            t_a1 = time.perf_counter()
            sum_getattr_s += (t_a1 - t_a0)

            t_n0 = time.perf_counter()
            ct = 0
            for _nb_gid in pr.iterate_neighbors(gid):
                ct += 1
            t_n1 = time.perf_counter()
            sum_neighbors_loop_s += (t_n1 - t_n0)

            t_ent1 = time.perf_counter()
            ent_dt = (t_ent1 - t_ent0)
            sum_entity_s += ent_dt

            total_entities += 1
            total_neighbors += ct

            if total_entities % 1000 == 0:
                mean_ent_ms = 1e3 * (sum_entity_s / total_entities)
                mean_attr_ms = 1e3 * (sum_getattr_s / total_entities)
                mean_nb_us = (1e6 * (sum_neighbors_loop_s / total_neighbors)) if total_neighbors else 0.0
                mean_nb_per_ent = (total_neighbors / total_entities) if total_entities else 0.0

                elapsed = time.perf_counter() - t0
                print(
                    f"{total_entities} gid={gid} citype={citype} name={name!r} nbs={ct} | "
                    f"elapsed={elapsed:.2f}s mean/entity={mean_ent_ms:.3f}ms "
                    f"(attr={mean_attr_ms:.3f}ms) mean/neighbor={mean_nb_us:.2f}µs "
                    f"avg_nbs/entity={mean_nb_per_ent:.2f} "
                    f"{needed_open_partitions(pr, citype, role="either")}"
                )

    t1 = time.perf_counter()
    total_s = t1 - t0

    mean_ent_ms = 1e3 * (sum_entity_s / total_entities) if total_entities else 0.0
    mean_attr_ms = 1e3 * (sum_getattr_s / total_entities) if total_entities else 0.0
    mean_nb_us = (1e6 * (sum_neighbors_loop_s / total_neighbors)) if total_neighbors else 0.0
    mean_nb_per_ent = (total_neighbors / total_entities) if total_entities else 0.0

    print("\n--- Summary ---")
    print(f"entities: {total_entities}")
    print(f"neighbors visited: {total_neighbors} (avg {mean_nb_per_ent:.2f} per entity)")
    print(f"total time: {total_s:.3f}s")
    print(f"mean time per entity (incl. neighbors): {mean_ent_ms:.3f} ms")
    print(f"mean time in get_attr_value per entity: {mean_attr_ms:.3f} ms")
    print(f"mean time per neighbor iteration: {mean_nb_us:.2f} µs")
    print(f"time accounted in entity body: {sum_entity_s:.3f}s ({100.0 * sum_entity_s / total_s:.1f}% of total)")


if __name__ == "__main__":
    main()