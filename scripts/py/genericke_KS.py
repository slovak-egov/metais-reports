#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from uuid import UUID

from rapidfuzz import fuzz, process

from metais.packed_reader.packed_reader import PackedReader
from metais.common.project_root import find_project_root
from metais.common.date import find_latest_dump


UUID_DEUS = "d88e14ac-d978-4666-bbe7-8ef82a9b3654"
UUID_OAM  = "fe36d0c7-4028-414c-b5f5-994f0bb48c7c"


def _uuid_hi_lo(u: str) -> tuple[int, int]:
    x = UUID(u).int
    return (x >> 64) & ((1 << 64) - 1), x & ((1 << 64) - 1)


DEUS_HI, DEUS_LO = _uuid_hi_lo(UUID_DEUS)
OAM_HI,  OAM_LO  = _uuid_hi_lo(UUID_OAM)


def _uuid_str_from_hi_lo(hi: int, lo: int) -> str:
    return str(UUID(int=((int(hi) << 64) | int(lo))))


def load_json_file(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# -----------------------------
# Layout node model
# -----------------------------
@dataclass
class LayNode:
    gid: int
    label: str
    bucket: str           # "OAM" | "DEUS" | "DCOM"

    # KS fields
    metais_code: str = ""
    uuid_str: str = ""

    # DCOM fields
    dcom_group: str = ""
    dcom_link: str = ""
    match_to_gid: Optional[int] = None
    match_score: float = 0.0

    # geometry
    w: float = 0.0
    h: float = 0.0
    y: float = 0.0


def estimate_box(line1: str, line2: str) -> Tuple[float, float]:
    h = 46.0
    L = max(len(line1), len(line2), 1)
    w = max(140.0, min(540.0, 7.4 * L + 40.0))
    return w, h


def xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;"))

def _approx_text_px(s: str, font_size: float) -> float:
    """
    Rough pixel width estimate for Segoe UI / Arial-ish fonts.
    Works well enough for ellipsizing.
    """
    # Average glyph width ~0.55–0.60 of font size
    return len(s) * font_size * 0.56

def ellipsize_px(s: str, max_px: float, font_size: float, suffix: str = "...") -> str:
    s = (s or "").strip()
    if not s:
        return s
    if _approx_text_px(s, font_size) <= max_px:
        return s

    suf = suffix
    suf_px = _approx_text_px(suf, font_size)
    if suf_px >= max_px:
        return suf  # extreme edge case (tiny box)

    # Binary search for largest prefix that fits with suffix
    lo, hi = 0, len(s)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        cand = s[:mid].rstrip() + suf
        if _approx_text_px(cand, font_size) <= max_px:
            lo = mid
        else:
            hi = mid - 1

    return s[:lo].rstrip() + suf


def _strip_accents(s: str) -> str:
    s = unicodedata.normalize("NFD", s)
    return "".join(ch for ch in s if unicodedata.category(ch) != "Mn")


_RX_NONALNUM = re.compile(r"[^0-9a-zA-Z]+", re.UNICODE)

def norm_name(s: str) -> str:
    s = (s or "").strip().lower()
    s = _strip_accents(s)
    s = _RX_NONALNUM.sub(" ", s)
    s = " ".join(s.split())
    return s


def _token_set(s: str) -> set[str]:
    return set(s.split()) if s else set()

def _jaccard(a: str, b: str) -> float:
    sa = _token_set(a)
    sb = _token_set(b)
    if not sa and not sb:
        return 1.0
    inter = len(sa & sb)
    uni = len(sa | sb)
    return inter / uni if uni else 0.0

def _custom_score(a: str, b: str) -> float:
    # Base similarity (no partial freebies)
    ts = float(fuzz.token_sort_ratio(a, b))  # 0..100

    # Penalize “same tail but lots of extra stuff”
    jac = _jaccard(a, b)                     # 0..1
    score = ts * jac

    # Optional: small penalty if first word differs (often the action verb)
    a0 = a.split()[0] if a else ""
    b0 = b.split()[0] if b else ""
    if a0 and b0 and a0 != b0:
        score -= 6.0

    return max(0.0, score)

def best_two_matches(query_norm: str, choices_by_gid_norm: Dict[int, str]) -> List[Tuple[int, float]]:
    if not query_norm:
        return []
    res = process.extract(
        query_norm,
        choices_by_gid_norm,   # gid -> normalized string
        scorer=_custom_score,
        limit=2,
    )
    out: List[Tuple[int, float]] = []
    for _match_str, score, gid in res:
        out.append((int(gid), float(score)))
    return out


# -----------------------------
# Deterministic 2-column layout for OAM/DEUS (unchanged intent)
# -----------------------------
def layout_two_columns_deterministic(
    nodes: Dict[int, LayNode],
    edges: List[Tuple[int, int]],
    *,
    min_gap: float = 14.0,
) -> None:
    # Ensure boxes known
    for n in nodes.values():
        if n.w <= 0 or n.h <= 0:
            line2 = f"{n.metais_code}, {n.uuid_str}".strip().strip(",")
            n.w, n.h = estimate_box(n.label, line2)

    # Unique undirected edges
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

    # 1) Cross-bucket pairs first
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
        cross_pairs.append((oa, de))
        used.add(oa); used.add(de)

    cross_pairs.sort(key=lambda p: (nodes[p[0]].label.lower(), p[0], nodes[p[1]].label.lower(), p[1]))

    # 2) Intra pairs next
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

    # 3) Singles last
    oam_singles = [gid for gid, n in nodes.items() if n.bucket == "OAM"  and gid not in used]
    deus_singles = [gid for gid, n in nodes.items() if n.bucket == "DEUS" and gid not in used]
    oam_singles.sort(key=lambda g: (nodes[g].label.lower(), g))
    deus_singles.sort(key=lambda g: (nodes[g].label.lower(), g))

    rows: List[Tuple[Optional[int], Optional[int]]] = []

    for oa, de in cross_pairs:
        rows.append((oa, de))

    m = max(len(oam_pairs), len(deus_pairs))
    for i in range(m):
        op = oam_pairs[i] if i < len(oam_pairs) else None
        dp = deus_pairs[i] if i < len(deus_pairs) else None
        rows.append((op[0] if op else None, dp[0] if dp else None))
        rows.append((op[1] if op else None, dp[1] if dp else None))

    m = max(len(oam_singles), len(deus_singles))
    for i in range(m):
        og = oam_singles[i] if i < len(oam_singles) else None
        dg = deus_singles[i] if i < len(deus_singles) else None
        rows.append((og, dg))

    # Assign y top->bottom
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
        if dg is not None:
            nodes[dg].y = y
        y += 0.5 * row_h + min_gap

    # Recenter around 0
    if nodes:
        mean = sum(n.y for n in nodes.values()) / len(nodes)
        for n in nodes.values():
            n.y -= mean


# -----------------------------
# Place DCOM nodes:
# - matched align to KS y
# - unmatched go below everything
# -----------------------------
def place_dcom_nodes_even_grid(
    nodes_all: Dict[int, LayNode],
    *,
    min_gap: float = 14.0,
    phase_frac: float = 0.5,  # 0.0 = same rows as KS, 0.5 = between rows
) -> None:
    """
    Place DCOM nodes on an evenly spaced grid derived from KS row spacing.

    - KS rows define a step (median delta of unique KS y's).
    - DCOM nodes get y = base + slot*step where base = first_row_y + phase_frac*step.
    - Matched DCOM nodes go into the slot of their matched KS row index (collision bumps down).
    - Unmatched go after all matched, continuing the same grid.
    """

    # Ensure DCOM boxes known
    for n in nodes_all.values():
        if n.bucket == "DCOM" and (n.w <= 0 or n.h <= 0):
            n.w, n.h = estimate_box(n.label, (n.dcom_group or "").strip())

    # Collect unique KS row y's
    ks_row_ys = sorted({round(n.y, 6) for n in nodes_all.values() if n.bucket in ("OAM", "DEUS")})
    if not ks_row_ys:
        return

    # Derive grid step from KS rows (median diff). Fallback to h + 2*gap.
    if len(ks_row_ys) >= 2:
        diffs = [ks_row_ys[i + 1] - ks_row_ys[i] for i in range(len(ks_row_ys) - 1)]
        diffs.sort()
        step = diffs[len(diffs) // 2]
        # Safety fallback if something weird happens (shouldn't)
        if step <= 0:
            step = 46.0 + 2.0 * min_gap
    else:
        step = 46.0 + 2.0 * min_gap

    # Phase shift: 0.5 puts DCOM between KS rows
    base_y = ks_row_ys[0] + phase_frac * step

    # Map KS row y -> row index
    y_to_row = {y: i for i, y in enumerate(ks_row_ys)}

    # Collect DCOM nodes
    dcom_nodes = [n for n in nodes_all.values() if n.bucket == "DCOM"]
    if not dcom_nodes:
        return

    matched: List[Tuple[int, LayNode]] = []
    unmatched: List[LayNode] = []

    for n in dcom_nodes:
        if n.match_to_gid is None or n.match_to_gid not in nodes_all:
            unmatched.append(n)
            continue
        ky = round(nodes_all[n.match_to_gid].y, 6)
        r = y_to_row.get(ky)
        if r is None:
            unmatched.append(n)
            continue
        matched.append((r, n))

    # Deterministic ordering
    matched.sort(key=lambda t: (t[0], -t[1].match_score, t[1].label.lower()))
    unmatched.sort(key=lambda n: (n.dcom_group.lower(), n.label.lower()))

    occupied: set[int] = set()
    max_slot = -1

    # Place matched first, collision bumps down to next free slot
    for r, n in matched:
        slot = r
        while slot in occupied:
            slot += 1
        n.y = base_y + slot * step
        occupied.add(slot)
        if slot > max_slot:
            max_slot = slot

    # Place unmatched after matched, continuing the same grid
    slot = max_slot + 1 if max_slot >= 0 else 0
    for n in unmatched:
        while slot in occupied:
            slot += 1
        n.y = base_y + slot * step
        occupied.add(slot)
        slot += 1


# -----------------------------
# SVG writer (3 columns, no big frames)
# Solid edges: KS_realizuje_KS
# Dashed edges: inferred DCOM->KS
# -----------------------------
def write_svg_three_columns(
    nodes: Dict[int, LayNode],
    edges_solid: List[Tuple[int, int]],
    edges_dashed: List[Tuple[int, int]],
    out_path: str,
    *,
    x_oam: float  = 220.0,
    x_deus: float = 980.0,
    x_dcom: float = 1740.0,
    margin_y: float = 90.0,
    pad_x: float = 10.0,
    inward: float = 95.0,
) -> None:
    def x_of_bucket(bucket: str) -> float:
        if bucket == "OAM":
            return x_oam
        if bucket == "DEUS":
            return x_deus
        return x_dcom

    ys = [n.y for n in nodes.values()]
    if not ys:
        raise ValueError("No nodes")

    # shift so min y starts at margin_y
    miny0 = min(ys)
    shift_y = margin_y - miny0
    for n in nodes.values():
        n.y += shift_y

    miny = min(n.y for n in nodes.values())
    maxy = max(n.y for n in nodes.values())

    # --- robust viewBox based on real node extents ---
    def cx_of(n: LayNode) -> float:
        return x_of_bucket(n.bucket)

    # bounds in content coordinates (after y-shift above)
    min_x = float("inf")
    max_x = float("-inf")
    min_y = float("inf")
    max_y2 = float("-inf")

    for n in nodes.values():
        cx = cx_of(n)
        x0 = cx - 0.5 * n.w
        x1 = cx + 0.5 * n.w
        y0 = n.y - 0.5 * n.h
        y1 = n.y + 0.5 * n.h
        if x0 < min_x: min_x = x0
        if x1 > max_x: max_x = x1
        if y0 < min_y: min_y = y0
        if y1 > max_y2: max_y2 = y1

    # add margins so arrowheads / curves also fit
    pad_x_eff = pad_x
    pad_y_eff = margin_y

    view_x = min_x - pad_x_eff
    view_y = min_y - pad_y_eff
    width  = (max_x - min_x) + 2 * pad_x_eff
    height = (max_y2 - min_y) + 2 * pad_y_eff

    def box(n: LayNode):
        cx = x_of_bucket(n.bucket)
        cy = n.y
        return (cx - n.w/2, cy - n.h/2, cx + n.w/2, cy + n.h/2)

    def edge_anchors(na: LayNode, nb: LayNode) -> Tuple[float, float, float, float]:
        ax0, ay0, ax1, ay1 = box(na)
        bx0, by0, bx1, by1 = box(nb)
        xa = x_of_bucket(na.bucket)
        xb = x_of_bucket(nb.bucket)

        if xb > xa:
            x1 = ax1
            x2 = bx0
        elif xb < xa:
            x1 = ax0
            x2 = bx1
        else:
            # same column: anchor on "inner-ish" side (we’ll bow anyway)
            x1 = ax1
            x2 = bx1
        return x1, na.y, x2, nb.y

    def path_d(na: LayNode, nb: LayNode) -> str:
        x1, y1, x2, y2 = edge_anchors(na, nb)
        xa = x_of_bucket(na.bucket)
        xb = x_of_bucket(nb.bucket)

        if xa == xb:
            # intra column: bow toward global center
            x_mid = 0.5 * (x_oam + x_dcom)
            sgn = 1.0 if (x_mid - xa) > 0 else (-1.0 if (x_mid - xa) < 0 else 1.0)
            out = sgn * inward
            cx1 = x1 + out
            cx2 = x2 + out
            return f'M {x1:.1f},{y1:.1f} C {cx1:.1f},{y1:.1f} {cx2:.1f},{y2:.1f} {x2:.1f},{y2:.1f}'

        cx1 = x1 + 0.35 * (x2 - x1)
        cx2 = x1 + 0.65 * (x2 - x1)
        return f'M {x1:.1f},{y1:.1f} C {cx1:.1f},{y1:.1f} {cx2:.1f},{y2:.1f} {x2:.1f},{y2:.1f}'

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'width="{width:.0f}" height="{height:.0f}" '
            f'viewBox="{view_x:.1f} {view_y:.1f} {width:.1f} {height:.1f}" '
            f'preserveAspectRatio="xMinYMin meet">\n'
        )

        # arrow marker
        f.write('  <defs>\n')
        f.write('    <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" '
                'markerWidth="6" markerHeight="6" orient="auto">\n')
        f.write('      <path d="M 0 0 L 10 5 L 0 10 z" fill="#666"/>\n')
        f.write('    </marker>\n')
        f.write('  </defs>\n')

        # headers only (no big frames)
        f.write(f'  <text x="{x_oam:.1f}" y="{margin_y-22:.1f}" text-anchor="middle" '
                f'font-family="Segoe UI, Arial" font-size="18">OAM</text>\n')
        f.write(f'  <text x="{x_deus:.1f}" y="{margin_y-22:.1f}" text-anchor="middle" '
                f'font-family="Segoe UI, Arial" font-size="18">DEUS</text>\n')
        f.write(f'  <text x="{x_dcom:.1f}" y="{margin_y-22:.1f}" text-anchor="middle" '
                f'font-family="Segoe UI, Arial" font-size="18">DCOM</text>\n')

        # solid edges
        f.write('  <g fill="none" stroke="#666" stroke-width="1.4" opacity="0.78" marker-end="url(#arrow)">\n')
        for a, b in edges_solid:
            if a not in nodes or b not in nodes or a == b:
                continue
            f.write(f'    <path d="{path_d(nodes[a], nodes[b])}"/>\n')
        f.write('  </g>\n')

        # dashed inferred edges
        f.write('  <g fill="none" stroke="#666" stroke-width="1.2" opacity="0.55" '
                'stroke-dasharray="6 4" marker-end="url(#arrow)">\n')
        for a, b in edges_dashed:
            if a not in nodes or b not in nodes or a == b:
                continue
            f.write(f'    <path d="{path_d(nodes[a], nodes[b])}"/>\n')
        f.write('  </g>\n')

        # nodes (two lines)
        f.write('  <g font-family="Segoe UI, Arial">\n')
        for n in nodes.values():
            x0, y0, x1, y1 = box(n)
            tx = x_of_bucket(n.bucket)

            f.write(f'    <rect x="{x0:.1f}" y="{y0:.1f}" width="{(x1-x0):.1f}" height="{(y1-y0):.1f}" '
                    f'rx="10" ry="10" fill="white" stroke="#222"/>\n')

            # --- ellipsize to fit inside the box ---
            PAD_TEXT = 26.0  # pixels of horizontal padding inside box (tweak if you want)
            max_px_1 = max(10.0, n.w - PAD_TEXT)
            max_px_2 = max(10.0, n.w - PAD_TEXT)

            line1_full = (n.label or "").strip()
            line1_fit  = ellipsize_px(line1_full, max_px_1, font_size=12.0, suffix="...")

            if n.bucket == "DCOM":
                line2_full = (n.dcom_group or "").strip()
            else:
                line2_full = f"{n.metais_code}, {n.uuid_str}".strip().strip(",")

            line2_fit = ellipsize_px(line2_full, max_px_2, font_size=10.0, suffix="...")

            line1 = xml_escape(line1_fit)
            line2 = xml_escape(line2_fit)

            # Tooltip on hover if truncated
            need_title = (line1_fit != line1_full) or (line2_fit != line2_full)
            title_text = xml_escape(line1_full + ("\n" + line2_full if line2_full else ""))

            # Use tspans (two lines), centered
            tx = x_of_bucket(n.bucket)
            ty = n.y - 6.0
            f.write(f'    <text x="{tx:.1f}" y="{ty:.1f}" text-anchor="middle">\n')
            if need_title:
                f.write(f'      <title>{title_text}</title>\n')
            f.write(f'      <tspan x="{tx:.1f}" dy="0" font-size="12">{line1}</tspan>\n')
            if line2:
                f.write(f'      <tspan x="{tx:.1f}" dy="14" font-size="10">{line2}</tspan>\n')
            f.write('    </text>\n')

        f.write('  </g>\n')
        f.write('</svg>\n')


# -----------------------------
# KS graph extraction (nodes + solid edges)
# -----------------------------
def build_ks_graph(pr: PackedReader) -> tuple[Dict[int, LayNode], List[Tuple[int, int]]]:
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
            gid=int(gid),
            label=str(label),
            bucket=bucket,
            metais_code=str(metais_code) if metais_code is not None else "",
            uuid_str=uuid_str,
        )

        line2 = f"{ln.metais_code}, {ln.uuid_str}".strip().strip(",")
        ln.w, ln.h = estimate_box(ln.label, line2)
        nodes[int(gid)] = ln

    edges: List[Tuple[int, int]] = []
    seen: set[Tuple[int, int]] = set()

    for src_gid, tgt_gid, relid in pr.iterate_relations(
        reltype="KS_realizuje_KS",
        src_citype="KS",
        tgt_citype="KS",
        include_relid=True,
        valid_only=True,
    ):
        if int(src_gid) in nodes and int(tgt_gid) in nodes:
            e = (int(src_gid), int(tgt_gid))
            if e not in seen:
                seen.add(e)
                edges.append(e)

    return nodes, edges


# -----------------------------
# DCOM: add nodes + inferred dashed edges
# -----------------------------
def add_dcom_column(
    nodes: Dict[int, LayNode],
    dcom_data: dict,
    *,
    ts_threshold: float = 95.0,
    jacc_threshold: float = 0.70,
    second_match_delta: float = 0.7,   # compare effective scores
) -> List[Tuple[int, int]]:
    arr = dcom_data.get("arr", [])
    services: List[Tuple[str, str, str]] = []  # (group, name, link)
    for g in arr:
        group = str(g.get("group", "") or "")
        for s in (g.get("services") or []):
            name = str(s.get("name", "") or "").strip()
            link = str(s.get("link", "") or "").strip()
            if name:
                services.append((group, name, link))

    ks_choices_norm: Dict[int, str] = {
        gid: norm_name(n.label)
        for gid, n in nodes.items()
        if n.bucket in ("OAM", "DEUS")
    }

    edges_dashed: List[Tuple[int, int]] = []
    seen_dashed: set[Tuple[int, int]] = set()

    next_gid = -1
    for group, name, link in services:
        qn = norm_name(name)

        res = process.extract(
            qn,
            ks_choices_norm,              # gid -> norm string
            scorer=fuzz.token_sort_ratio, # no partial freebies
            limit=2,
        )

        best_gid: Optional[int] = None
        best_eff: float = 0.0

        if res:
            _s0, ts0, gid0 = res[0]
            gid0 = int(gid0)
            ts0 = float(ts0)
            jac0 = _jaccard(qn, ks_choices_norm[gid0])
            eff0 = ts0 * jac0

            ok0 = (ts0 >= ts_threshold and jac0 >= jacc_threshold)
            if ok0:
                best_gid = gid0
                best_eff = eff0

        ln = LayNode(
            gid=next_gid,
            label=name,
            bucket="DCOM",
            dcom_group=group,
            dcom_link=link,
            match_to_gid=best_gid,
            match_score=best_eff if best_gid is not None else 0.0,
        )
        ln.w, ln.h = estimate_box(ln.label, ln.dcom_group.strip())
        nodes[next_gid] = ln

        # add best edge
        if best_gid is not None:
            e = (int(next_gid), int(best_gid))
            if e not in seen_dashed:
                seen_dashed.add(e)
                edges_dashed.append(e)

            # optional 2nd edge if near tie, but apply same gating + compute effective score
            if len(res) >= 2:
                _s1, ts1, gid1 = res[1]
                gid1 = int(gid1)
                ts1 = float(ts1)
                jac1 = _jaccard(qn, ks_choices_norm[gid1])
                eff1 = ts1 * jac1

                ok1 = (ts1 >= ts_threshold and jac1 >= jacc_threshold)

                if ok1 and gid1 != best_gid and (best_eff - eff1) <= second_match_delta:
                    e2 = (int(next_gid), int(gid1))
                    if e2 not in seen_dashed:
                        seen_dashed.add(e2)
                        edges_dashed.append(e2)

        next_gid -= 1

    return edges_dashed


def main() -> None:
    proj_root = find_project_root()
    date = find_latest_dump(proj_root / "output")
    scratch = proj_root / "scratch"

    dcom_path = scratch / "dcom-services.json.2026-01-16"
    dcom_data = load_json_file(dcom_path)

    with PackedReader(
        date=date,
        dict_cache_size=16384,
        attr_cache_size=1024,
        resolver_cache_size=1024,
        open_relation_partitions_max=None
    ) as pr:
        nodes, edges_solid = build_ks_graph(pr)

        edges_dashed = add_dcom_column(
            nodes,
            dcom_data,
            ts_threshold=95.0,
            jacc_threshold=0.70,
            second_match_delta=0.7,
        )

        print(f"KS nodes: {sum(1 for n in nodes.values() if n.bucket in ('OAM','DEUS'))}, "
              f"DCOM nodes: {sum(1 for n in nodes.values() if n.bucket == 'DCOM')}")
        print(f"KS_realizuje_KS rels: {len(edges_solid)}")
        print(f"Matching name (DCOM->KS): {len(edges_dashed)}")

        # layout KS columns first
        ks_only = {gid: n for gid, n in nodes.items() if n.bucket in ("OAM", "DEUS")}
        layout_two_columns_deterministic(ks_only, edges_solid, min_gap=14.0)

        # DCOM placement uses those KS y positions
        place_dcom_nodes_even_grid(nodes, min_gap=14.0, phase_frac=0.5)

        out = scratch / "ks_oam_deus_dcom.svg"
        write_svg_three_columns(
            nodes,
            edges_solid,
            edges_dashed,
            str(out),
            x_oam=200.0,
            x_deus=800.0,
            x_dcom=1400.0,
            inward=95.0
        )
        print(f"Wrote {out}")


if __name__ == "__main__":
    main()
