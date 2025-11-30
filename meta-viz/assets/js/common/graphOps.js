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

export function buildDuplicateCliqueEdges(groups, relName = 'Has same MetaIS code') {
  const edges = [];
  (groups || []).forEach(g => {
    const primaries = g.entity_uuids || [];
    for (let i = 0; i < primaries.length; i++) {
      for (let j = i + 1; j < primaries.length; j++) {
        edges.push({
          source: primaries[i],
          target: primaries[j],
          kind:   'duplicate',
          relName,
        });
      }
    }
  });
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

// graphOps.js
export function buildSceneForNodeSetBase({
  uuidSet,
  baseNodes,
  allEdges,
  prevScene,
}) {
  if (!uuidSet || !uuidSet.size) return { nodes: [], edges: [] };

  const prevPos = new Map();
  for (const n of (prevScene?.nodes || [])) {
    prevPos.set(n.id, { x: n.x, y: n.y });
  }

  const nodes   = [];
  const nodeMap = new Map();

  function ensureNode(id) {
    if (!uuidSet.has(id)) return null;
    let node = nodeMap.get(id);
    if (node) return node;

    const base = baseNodes.get(id);
    if (!base) return null;

    const prev = prevPos.get(id);
    node = {
      ...base,
      id,
      x: prev ? prev.x : 0,
      y: prev ? prev.y : 0,
    };
    nodeMap.set(id, node);
    nodes.push(node);
    return node;
  }

  const edges = [];
  for (const e of allEdges || []) {
    if (!uuidSet.has(e.source) || !uuidSet.has(e.target)) continue;
    ensureNode(e.source);
    ensureNode(e.target);
    edges.push({ ...e });
  }

  return { nodes, edges };
}