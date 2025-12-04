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
  uuidSet,        // Set<string> of node IDs we want to show
  baseNodes,      // Map<uuid, { id, type, attrs, isPrimary, isInvalidated }>
  allEdges,       // Array<{ source, target, kind, relName, distance? }>
  prevScene       // { nodes, edges } from last frame, or null
}) {
  // ---- 1) Collect previous positions ---------------------------------

  const pos = new Map(); // uuid -> { x, y }

  if (prevScene && Array.isArray(prevScene.nodes)) {
    for (const n of prevScene.nodes) {
      if (n.id == null) continue;
      pos.set(n.id, { x: n.x ?? 0, y: n.y ?? 0 });
    }
  }

  const idSet = new Set(uuidSet);

  // ---- 2) Build adjacency for nodes we care about ---------------------

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

  // ---- 3) Bounding box of existing positions (for cluster placement) --

  let hasExisting = false;
  let minX = 0, maxX = 0, minY = 0, maxY = 0;

  for (const [id, p] of pos.entries()) {
    if (!idSet.has(id)) continue;
    if (!hasExisting) {
      hasExisting = true;
      minX = maxX = p.x;
      minY = maxY = p.y;
    } else {
      if (p.x < minX) minX = p.x;
      if (p.x > maxX) maxX = p.x;
      if (p.y < minY) minY = p.y;
      if (p.y > maxY) maxY = p.y;
    }
  }

  const cx   = hasExisting ? (minX + maxX) / 2 : 0;
  const cy   = hasExisting ? (minY + maxY) / 2 : 0;
  const span = hasExisting ? Math.max(maxX - minX, maxY - minY) : 10;

  // If we have no existing nodes at all, still give span a reasonable size
  const baseSpan = span || 10;

  let clusterIndex = 0;

  // ---- 4) Walk connected components, spawn new nodes per component ----

  const visited = new Set();

  for (const startId of idSet) {
    if (visited.has(startId)) continue;

    // BFS to get this component
    const queue = [startId];
    visited.add(startId);
    const component = [];

    while (queue.length) {
      const u = queue.pop();
      component.push(u);

      const nb = neighbors.get(u);
      if (!nb) continue;
      for (const v of nb) {
        if (!idSet.has(v)) continue;
        if (visited.has(v)) continue;
        visited.add(v);
        queue.push(v);
      }
    }

    if (!component.length) continue;

    // Split component nodes into "anchored" (already had positions) and "new"
    const anchored = [];
    const missing  = [];
    for (const id of component) {
      if (pos.has(id)) anchored.push(id);
      else             missing.push(id);
    }

    if (!missing.length) {
      // Everything in this component already has a position → nothing to spawn
      continue;
    }

    let anchorPos;

    if (anchored.length) {
      // Some nodes in this component already have positions:
      // anchor the new ones near the barycenter of those anchored nodes.
      let sx = 0, sy = 0;
      for (const id of anchored) {
        const p = pos.get(id);
        sx += p.x;
        sy += p.y;
      }
      anchorPos = {
        x: sx / anchored.length,
        y: sy / anchored.length,
      };
    } else {
      // Entire component is new: create a fresh cluster around the graph.
      // Place clusters on a ring around (cx, cy).
      const angle  = clusterIndex * (2 * Math.PI / 7);  // 7 “slots” on ring
      const radius = baseSpan * 0.8 + (clusterIndex + 1) * (baseSpan * 0.4 + 5);

      anchorPos = {
        x: cx + radius * Math.cos(angle),
        y: cy + radius * Math.sin(angle),
      };

      clusterIndex += 1;
    }

    // Place missing nodes around anchorPos in a small circle
    const nComp = missing.length;
    const compRadius = Math.max(baseSpan * 0.05, 2); // how “big” the new cluster is

    missing.forEach((id, idx) => {
      // If this node somehow got a position while processing earlier components, skip
      if (pos.has(id)) return;

      // Spread them evenly around the circle, plus a bit of jitter
      const theta = (nComp > 1)
        ? (idx / nComp) * 2 * Math.PI
        : 0;

      const jitterR = compRadius * 0.3 * (Math.random() - 0.5);
      const jitterT = (Math.random() - 0.5) * 0.5;

      const r = compRadius + jitterR;
      const a = theta + jitterT;

      pos.set(id, {
        x: anchorPos.x + r * Math.cos(a),
        y: anchorPos.y + r * Math.sin(a),
      });
    });
  }

  // ---- 5) Build node list ---------------------------------------------

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

  // For convenience, build a set of node IDs that survived
  const nodeIdSet = new Set(nodes.map(n => n.id));

  // ---- 6) Filter edges to only connect nodes we actually included -----

  const edges = [];
  for (const e of allEdges || []) {
    if (!nodeIdSet.has(e.source) || !nodeIdSet.has(e.target)) continue;
    edges.push(e);
  }

  return { nodes, edges };
}