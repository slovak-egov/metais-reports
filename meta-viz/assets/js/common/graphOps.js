export function buildRelationEdges(relsRaw) {
  const edges = [];
  for (const [relName, relInfo] of Object.entries(relsRaw || {})) {
    const pairs = relInfo.pairs || [];
    for (const [src, dst, distRaw] of pairs) {
      if (!src || !dst) continue;
      const distance =
        typeof distRaw === 'number' ? distRaw :
        (distRaw == null ? 0 : -1);
      edges.push({ source: src, target: dst, kind: 'relation', relName, distance });
    }
  }
  return edges;
}


export function buildAdjacency(edges) {
  const adj = new Map();
  const add = (a, b) => {
    if (!adj.has(a)) adj.set(a, new Set());
    adj.get(a).add(b);
  };

  for (const e of edges || []) {
    if (!e.source || !e.target) continue;
    add(e.source, e.target);
    add(e.target, e.source);
  }
  return adj;
}

export function collectNeighborsWithin(adjacency, nodeId, maxDist) {
  const result = new Set();
  if (!nodeId || maxDist < 0) return result;

  const visited = new Set([nodeId]);
  const queue   = [{ id: nodeId, dist: 0 }];

  while (queue.length) {
    const { id, dist } = queue.shift();
    result.add(id);
    if (dist >= maxDist) continue;

    const neigh = adjacency.get(id);
    if (!neigh) continue;

    for (const nb of neigh) {
      if (visited.has(nb)) continue;
      visited.add(nb);
      queue.push({ id: nb, dist: dist + 1 });
    }
  }
  return result;
}

/**
 * Build base scene (nodes + edges) for a given set of UUIDs.
 *
 * - Reuses positions from prevScene where possible.
 * - For new nodes, tries to spawn them near related nodes.
 * - Entirely new connected components are placed in separate clusters
 *   around the existing graph, instead of all at (0,0).
 */
export function buildSceneForNodeSetBase({
  uuidSet,        // Set<string>
  baseNodes,      // Map<uuid, { id, type, attrs, isPrimary, isInvalidated }>
  allEdges,       // Array<{ source, target, kind, relName, distance? }>
  prevScene,      // { nodes, edges } or null
}) {
  // ------------ 1) Previous positions (only for overlapping nodes) ----
  const pos = new Map(); // uuid -> { x, y }
  const idSet = new Set(uuidSet);

  let overlapCount = 0;

  if (prevScene && Array.isArray(prevScene.nodes)) {
    for (const n of prevScene.nodes) {
      if (n.id == null) continue;
      if (!idSet.has(n.id)) continue; // only reuse if in current set

      const x = Number.isFinite(n.x) ? n.x : 0;
      const y = Number.isFinite(n.y) ? n.y : 0;
      pos.set(n.id, { x, y });
      overlapCount++;
    }
  }

  const hasExisting = overlapCount > 0;

  // ------------ 2) Adjacency among visible nodes ----------------------
  const neighbors = new Map(); // uuid -> Set<uuid>
  for (const id of idSet) {
    neighbors.set(id, new Set());
  }

  for (const e of allEdges || []) {
    const { source, target } = e;
    if (!idSet.has(source) || !idSet.has(target)) continue;
    neighbors.get(source).add(target);
    neighbors.get(target).add(source);
  }

  // ------------ 3) Bounding box of already placed nodes ---------------
  let minX = 0, maxX = 0, minY = 0, maxY = 0;
  let bboxInitialized = false;

  for (const [id, p] of pos.entries()) {
    if (!bboxInitialized) {
      bboxInitialized = true;
      minX = maxX = p.x;
      minY = maxY = p.y;
    } else {
      if (p.x < minX) minX = p.x;
      if (p.x > maxX) maxX = p.x;
      if (p.y < minY) minY = p.y;
      if (p.y > maxY) maxY = p.y;
    }
  }

  const cx   = bboxInitialized ? (minX + maxX) / 2 : 0;
  const cy   = bboxInitialized ? (minY + maxY) / 2 : 0;
  const span = bboxInitialized ? Math.max(maxX - minX, maxY - minY) : 20;
  const baseSpan = span || 20;

  // ------------ 4) First pass: neighbor-based placement ----------------
  const missingInitial = [];
  for (const id of idSet) {
    if (!pos.has(id)) missingInitial.push(id);
  }

  let missing = new Set(missingInitial);
  let madeProgress = true;

  while (madeProgress && missing.size > 0) {
    madeProgress = false;

    for (const id of Array.from(missing)) {
      const nb = neighbors.get(id);
      if (!nb) continue;

      const pts = [];
      for (const v of nb) {
        const p = pos.get(v);
        if (p) pts.push(p);
      }

      if (!pts.length) continue;

      // average neighbor positions
      let sx = 0, sy = 0;
      for (const p of pts) {
        sx += p.x;
        sy += p.y;
      }
      const ax = sx / pts.length;
      const ay = sy / pts.length;

      // small jitter so they don't all stack
      const r = baseSpan * 0.03;
      const jx = (Math.random() - 0.5) * r;
      const jy = (Math.random() - 0.5) * r;

      pos.set(id, { x: ax + jx, y: ay + jy });
      missing.delete(id);
      madeProgress = true;
    }
  }

  // ------------ 5) Second pass: cluster truly new components -----------
  const stillMissing = new Set(missing);
  const components = [];

  while (stillMissing.size > 0) {
    const [startId] = stillMissing;
    stillMissing.delete(startId);

    const queue = [startId];
    const component = [startId];

    while (queue.length) {
      const u = queue.pop();
      const nb = neighbors.get(u) || new Set();
      for (const v of nb) {
        if (!stillMissing.has(v)) continue;
        stillMissing.delete(v);
        queue.push(v);
        component.push(v);
      }
    }

    components.push(component);
  }

  const numComponents = components.length || 1;

  // If we *had* existing nodes in this scene, drop new components around them.
  // If we had no overlap at all, this still uses cx=0,cy=0 and span≈20, so
  // new layouts start near world (0,0) (visually center of screen).
  const ringRadiusBase = hasExisting ? baseSpan * 0.25 : 0;
  const ringRadiusStep = hasExisting ? baseSpan * 0.18 : 10;

  components.forEach((component, idx) => {
    if (!component.length) return;

    const theta   = (2 * Math.PI * idx) / numComponents;
    const rCenter = ringRadiusBase + idx * ringRadiusStep;

    const anchor = {
      x: cx + rCenter * Math.cos(theta),
      y: cy + rCenter * Math.sin(theta),
    };

    const nComp = component.length;
    const baseRadius = hasExisting ? baseSpan * 0.12 : 8;
    const compRadius = baseRadius * Math.sqrt(nComp);

    component.forEach((id) => {
      if (pos.has(id)) return;

      const u = Math.random();
      const v = Math.random();
      const r = compRadius * Math.sqrt(u);
      const phi = 2 * Math.PI * v;

      pos.set(id, {
        x: anchor.x + r * Math.cos(phi),
        y: anchor.y + r * Math.sin(phi),
      });
    });
  });

  // ------------ 6) Build node list ------------------------------------
  const nodes = [];
  for (const id of idSet) {
    const base = baseNodes.get(id);
    if (!base) continue;

    const p = pos.get(id) || { x: 0, y: 0 };

    nodes.push({
      id:            base.id,
      type:          base.type,
      attrs:         base.attrs,
      isPrimary:     base.isPrimary,
      isInvalidated: base.isInvalidated,
      x:             p.x,
      y:             p.y,
    });
  }

  const nodeIdSet = new Set(nodes.map(n => n.id));

  // ------------ 7) Filter edges ---------------------------------------
  const edges = [];
  for (const e of allEdges || []) {
    if (!nodeIdSet.has(e.source) || !nodeIdSet.has(e.target)) continue;
    edges.push(e);
  }

  return { nodes, edges };
}