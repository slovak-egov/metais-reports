#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional
from uuid import UUID

from metais.packed_reader.packed_reader import PackedReader


UUID_DEUS = "d88e14ac-d978-4666-bbe7-8ef82a9b3654"
UUID_OAM  = "fe36d0c7-4028-414c-b5f5-994f0bb48c7c"


def _uuid_hi_lo(u: str) -> tuple[int, int]:
    x = UUID(u).int
    return (x >> 64) & ((1 << 64) - 1), x & ((1 << 64) - 1)


DEUS_HI, DEUS_LO = _uuid_hi_lo(UUID_DEUS)
OAM_HI,  OAM_LO  = _uuid_hi_lo(UUID_OAM)


def _uuid_str_from_hi_lo(hi: int, lo: int) -> str:
    return str(UUID(int=((int(hi) << 64) | int(lo))))


@dataclass
class LayNode:
    gid: int
    label: str
    bucket: str           # "DEUS" or "OAM"
    metais_code: str = ""
    uuid_str: str = ""
    w: float = 0.0
    h: float = 0.0
    y: float = 0.0
    vy: float = 0.0       # unused now, but kept for compatibility


def estimate_box(line1: str, line2: str) -> Tuple[float, float]:
    # Two text lines -> taller boxes
    h = 46.0
    # Width based on the longer line; allow it to grow, but keep under the frame
    L = max(len(line1), len(line2), 1)
    w = max(140.0, min(500.0, 7.4 * L + 40.0))
    return w, h


def xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;"))


# -----------------------------
# Deterministic two-column layout
# (CROSS first, then INTRA, then singles)
# -----------------------------
def layout_two_columns_deterministic(
    nodes: Dict[int, LayNode],
    edges: List[Tuple[int, int]],
    *,
    min_gap: float = 14.0,
) -> None:
    """
    Deterministic layout specialized for the "max-cardinality-1" case, in this order:

    1) Cross-bucket (OAM<->DEUS) pairs first: each pair shares the same row (same y).
    2) Within-bucket pairs next (stacked A above B), balanced left/right when possible.
    3) Remaining singletons last, balanced left/right.

    Produces a shared global row stack so the two columns stay visually aligned.
    """

    # Ensure boxes known
    for n in nodes.values():
        if n.w <= 0 or n.h <= 0:
            line2 = f"{n.metais_code}, {n.uuid_str}".strip().strip(",")
            n.w, n.h = estimate_box(n.label, line2)

    # Unique edges in an undirected sense for pairing decisions
    und_seen: set[Tuple[int, int]] = set()
    uniq_und: List[Tuple[int, int]] = []
    for a, b in edges:
        if a not in nodes or b not in nodes or a == b:
            continue
        k = (a, b) if a < b else (b, a)
        if k in und_seen:
            continue
        und_seen.add(k)
        uniq_und.append((a, b))

    uniq_und.sort(key=lambda e: (min(e[0], e[1]), max(e[0], e[1]), e[0], e[1]))

    used: set[int] = set()

    # 1) CROSS pairs first
    cross_pairs: List[Tuple[int, int]] = []  # (oam_gid, deus_gid)
    for a, b in uniq_und:
        if a in used or b in used:
            continue
        ba = nodes[a].bucket
        bb = nodes[b].bucket
        if ba == bb:
            continue
        oa = a if ba == "OAM" else b
        de = b if oa == a else a
        if oa in used or de in used:
            continue
        cross_pairs.append((oa, de))
        used.add(oa); used.add(de)

    cross_pairs.sort(key=lambda p: (nodes[p[0]].label.lower(), p[0], nodes[p[1]].label.lower(), p[1]))

    # 2) INTRA pairs next, on remaining nodes
    def take_intra_pairs(bucket: str) -> List[Tuple[int, int]]:
        pairs: List[Tuple[int, int]] = []
        for a, b in uniq_und:
            if a in used or b in used:
                continue
            if nodes[a].bucket != bucket or nodes[b].bucket != bucket:
                continue
            pairs.append((a, b))
            used.add(a); used.add(b)
        pairs.sort(key=lambda p: (min(p[0], p[1]), max(p[0], p[1])))
        return pairs

    oam_pairs  = take_intra_pairs("OAM")
    deus_pairs = take_intra_pairs("DEUS")

    # 3) Remaining singletons
    oam_singles = [gid for gid, n in nodes.items() if n.bucket == "OAM"  and gid not in used]
    deus_singles = [gid for gid, n in nodes.items() if n.bucket == "DEUS" and gid not in used]
    oam_singles.sort(key=lambda g: (nodes[g].label.lower(), g))
    deus_singles.sort(key=lambda g: (nodes[g].label.lower(), g))

    # Build global rows: (oam_gid_or_None, deus_gid_or_None)
    rows: List[Tuple[Optional[int], Optional[int]]] = []

    # Cross rows first (aligned)
    for oa, de in cross_pairs:
        rows.append((oa, de))

    # Intra blocks next (2 rows per pair), balanced
    m = max(len(oam_pairs), len(deus_pairs))
    for i in range(m):
        op = oam_pairs[i] if i < len(oam_pairs) else None
        dp = deus_pairs[i] if i < len(deus_pairs) else None
        rows.append((op[0] if op else None, dp[0] if dp else None))
        rows.append((op[1] if op else None, dp[1] if dp else None))

    # Singles last, balanced
    m = max(len(oam_singles), len(deus_singles))
    for i in range(m):
        og = oam_singles[i] if i < len(oam_singles) else None
        dg = deus_singles[i] if i < len(deus_singles) else None
        rows.append((og, dg))

    # Assign y top->bottom, variable row height
    y = 0.0
    for og, dg in rows:
        hs: List[float] = []
        if og is not None: hs.append(nodes[og].h)
        if dg is not None: hs.append(nodes[dg].h)
        if not hs:
            continue
        row_h = max(hs)

        y += 0.5 * row_h + min_gap
        if og is not None:
            nodes[og].y = y
            nodes[og].vy = 0.0
        if dg is not None:
            nodes[dg].y = y
            nodes[dg].vy = 0.0
        y += 0.5 * row_h + min_gap

    # Recenter around 0
    if nodes:
        mean = sum(n.y for n in nodes.values()) / len(nodes)
        for n in nodes.values():
            n.y -= mean


# -----------------------------
# SVG writer (OAM left, DEUS right, INSIDE routing only)
# + second line: metais_code, uuid
# -----------------------------
def write_svg_two_columns(
    nodes: Dict[int, LayNode],
    edges: List[Tuple[int, int]],
    out_path: str = "ks_realizuje.svg",
    *,
    x_oam: float  = 220.0,   # LEFT
    x_deus: float = 980.0,   # RIGHT
    margin_y: float = 90.0,
    pad_x: float = 220.0,
    inward: float = 95.0,    # how far intra edges bow toward the center
) -> None:
    def x_of(n: LayNode) -> float:
        return x_oam if n.bucket == "OAM" else x_deus

    ys = [n.y for n in nodes.values()]
    if not ys:
        raise ValueError("No nodes")

    miny = min(ys)
    shift_y = margin_y - miny
    for n in nodes.values():
        n.y += shift_y

    ys = [n.y for n in nodes.values()]
    miny = min(ys)
    maxy = max(ys)

    width  = (x_deus - x_oam) + 2 * pad_x
    height = (maxy - miny) + 2 * margin_y
    view_x = x_oam - pad_x
    view_y = 0.0

    def box(n: LayNode):
        cx = x_of(n)
        cy = n.y
        return (cx - n.w/2, cy - n.h/2, cx + n.w/2, cy + n.h/2)

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'width="{width:.0f}" height="{height:.0f}" '
            f'viewBox="{view_x:.0f} {view_y:.0f} {width:.0f} {height:.0f}">\n'
        )

        # arrow marker
        f.write('  <defs>\n')
        f.write('    <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" '
                'markerWidth="6" markerHeight="6" orient="auto">\n')
        f.write('      <path d="M 0 0 L 10 5 L 0 10 z" fill="#666"/>\n')
        f.write('    </marker>\n')
        f.write('  </defs>\n')

        # column frames
        frame_w = 520.0
        f.write(f'  <rect x="{x_oam-frame_w/2:.1f}" y="{margin_y:.1f}" width="{frame_w:.1f}" height="{height-2*margin_y:.1f}" '
                f'rx="18" ry="18" fill="none" stroke="#999"/>\n')
        f.write(f'  <rect x="{x_deus-frame_w/2:.1f}" y="{margin_y:.1f}" width="{frame_w:.1f}" height="{height-2*margin_y:.1f}" '
                f'rx="18" ry="18" fill="none" stroke="#999"/>\n')

        f.write(f'  <text x="{x_oam:.1f}" y="{margin_y-22:.1f}" text-anchor="middle" '
                f'font-family="Segoe UI, Arial" font-size="18">OAM</text>\n')
        f.write(f'  <text x="{x_deus:.1f}" y="{margin_y-22:.1f}" text-anchor="middle" '
                f'font-family="Segoe UI, Arial" font-size="18">DEUS</text>\n')

        # edges (inside-only routing)
        f.write('  <g fill="none" stroke="#666" stroke-width="1.4" opacity="0.75" marker-end="url(#arrow)">\n')
        for a, b in edges:
            if a not in nodes or b not in nodes or a == b:
                continue
            na = nodes[a]
            nb = nodes[b]

            ax0, ay0, ax1, ay1 = box(na)
            bx0, by0, bx1, by1 = box(nb)

            def inner_x(n: LayNode, x0: float, x1: float) -> float:
                # OAM inner is right edge; DEUS inner is left edge
                return x1 if n.bucket == "OAM" else x0

            if na.bucket != nb.bucket:
                # cross-column: inner->inner
                x1 = inner_x(na, ax0, ax1)
                y1 = na.y
                x2 = inner_x(nb, bx0, bx1)
                y2 = nb.y

                cx1 = x1 + 0.35 * (x2 - x1)
                cx2 = x1 + 0.65 * (x2 - x1)
                f.write(f'    <path d="M {x1:.1f},{y1:.1f} C {cx1:.1f},{y1:.1f} {cx2:.1f},{y2:.1f} {x2:.1f},{y2:.1f}"/>\n')
            else:
                # intra-column: also bow inward (toward center gap)
                x1 = inner_x(na, ax0, ax1)
                y1 = na.y
                x2 = inner_x(nb, bx0, bx1)
                y2 = nb.y

                out = +inward if na.bucket == "OAM" else -inward
                cx1 = x1 + out
                cx2 = x2 + out
                f.write(f'    <path d="M {x1:.1f},{y1:.1f} C {cx1:.1f},{y1:.1f} {cx2:.1f},{y2:.1f} {x2:.1f},{y2:.1f}"/>\n')
        f.write('  </g>\n')

        # nodes (two lines of text)
        f.write('  <g font-family="Segoe UI, Arial">\n')
        for n in nodes.values():
            x0, y0, x1, y1 = box(n)

            f.write(f'    <rect x="{x0:.1f}" y="{y0:.1f}" width="{(x1-x0):.1f}" height="{(y1-y0):.1f}" '
                    f'rx="10" ry="10" fill="white" stroke="#222"/>\n')

            line1 = xml_escape(n.label)
            line2_raw = f"{n.metais_code}, {n.uuid_str}".strip()
            line2_raw = line2_raw.strip().strip(",")
            line2 = xml_escape(line2_raw)

            # Use tspans for two lines, centered
            # Baseline placed a bit above center so the two lines fit nicely.
            tx = x_of(n)
            ty = n.y - 6.0
            f.write(f'    <text x="{tx:.1f}" y="{ty:.1f}" text-anchor="middle">\n')
            f.write(f'      <tspan x="{tx:.1f}" dy="0" font-size="12">{line1}</tspan>\n')
            f.write(f'      <tspan x="{tx:.1f}" dy="14" font-size="10">{line2}</tspan>\n')
            f.write('    </text>\n')

        f.write('  </g>\n')
        f.write('</svg>\n')


# -----------------------------
# Graph extraction (adds metais_code + uuid_str)
# -----------------------------
def build_graph(pr: PackedReader) -> tuple[Dict[int, LayNode], List[Tuple[int, int]], List[Tuple[int, int, int]]]:
    KS_DEUS: set[int] = set()
    KS_OAM: set[int] = set()
    ks_included: set[int] = set()

    uuid_str_by_gid: Dict[int, str] = {}

    for ks in pr.iterate_citype("KS", include_attrs=False, include_meta=False, valid_only=True):
        gid = ks.gid

        uuid_str_by_gid[gid] = _uuid_str_from_hi_lo(ks.uuid_hi, ks.uuid_lo)

        generic = bool(pr.get_attr_value_typed(ks, "KS_Profil_UPVS_je_genericka", default=False))

        has_deus = False
        has_oam  = False
        for po in pr.iterate_neighbors(
            ks,
            reltype="PO_je_gestor_KS",
            role="target",
            include_attrs=False,
            as_nodes=True,
            valid_only=True,
        ):
            if po.uuid_hi == DEUS_HI and po.uuid_lo == DEUS_LO:
                has_deus = True
            if po.uuid_hi == OAM_HI and po.uuid_lo == OAM_LO:
                has_oam = True

        if has_deus:
            KS_DEUS.add(gid)
        if has_oam:
            KS_OAM.add(gid)

        if has_deus or generic:
            ks_included.add(gid)

    ks_all = ks_included & (KS_DEUS | KS_OAM)

    nodes: Dict[int, LayNode] = {}
    for gid in ks_all:
        label = pr.get_attr_value(gid, "Gen_Profil_nazov", default="(no name)")
        metais_code = pr.get_attr_value(gid, "Gen_Profil_kod_metais", default="")
        uuid_str = uuid_str_by_gid.get(gid, "")

        bucket = "DEUS" if gid in KS_DEUS else "OAM"

        ln = LayNode(
            gid=gid,
            label=str(label),
            bucket=bucket,
            metais_code=str(metais_code) if metais_code is not None else "",
            uuid_str=uuid_str,
        )

        line2 = f"{ln.metais_code}, {ln.uuid_str}".strip().strip(",")
        ln.w, ln.h = estimate_box(ln.label, line2)
        nodes[gid] = ln

    # edges same as before...
    edges: List[Tuple[int, int]] = []
    seen: set[Tuple[int, int]] = set()
    rels_kept: List[Tuple[int, int, int]] = []

    for src_gid, tgt_gid, relid in pr.iterate_relations(
        reltype="KS_realizuje_KS",
        src_citype="KS",
        tgt_citype="KS",
        include_relid=True,
        valid_only=True,  # True: 133, False: 137
    ):
        if src_gid in nodes and tgt_gid in nodes:
            e = (int(src_gid), int(tgt_gid))
            if e not in seen:
                seen.add(e)
                edges.append(e)
                rels_kept.append((int(src_gid), int(tgt_gid), int(relid)))

    return nodes, edges, rels_kept

def _node_state(pr: PackedReader, gid: int):
    citype, local = pr.gr.resolve_gid_full(gid)
    ar = pr._get_attr_reader(citype)
    if ar.meta_count == 0:
        return ("<no-meta>", citype, local)
    didx = ar.get_meta_cell(int(local), META_STATE_MIDX)
    if int(didx) == MISSING_I32:
        return (None, citype, local)
    return (pr.dict.get(int(didx)), citype, local)

def _rel_state(pr: PackedReader, reltype: str, relid: int):
    ar = pr._get_rel_attr_reader(reltype)
    if ar is None or ar.meta_count == 0:
        return "<no-meta>"
    didx = ar.get_meta_cell(int(relid), META_STATE_MIDX)
    if int(didx) == MISSING_I32:
        return None
    return pr.dict.get(int(didx))

def main() -> None:
    with PackedReader(
        date="13-01-2026",
        dict_cache_size=16384,
        attr_cache_size=1024,
        resolver_cache_size=1024,
        open_relation_partitions_max=None
    ) as pr:
        nodes, edges, rels_kept = build_graph(pr)
        print(f"nodes: {len(nodes)}, edges: {len(edges)}")

        print("Sample relation URLs:")
        for i, (src_gid, tgt_gid, relid) in enumerate(rels_kept[:10], 1):
            url = pr.relation_url_by_relid("KS_realizuje_KS", relid)
            print(f"[{i}] relid={relid} {src_gid}->{tgt_gid}  url={url}")


        layout_two_columns_deterministic(nodes, edges, min_gap=14.0)

        out = "ks_realizuje.svg"
        write_svg_two_columns(nodes, edges, out, x_oam=220.0, x_deus=980.0)
        print(f"Wrote {out}")



if __name__ == "__main__":
    main()