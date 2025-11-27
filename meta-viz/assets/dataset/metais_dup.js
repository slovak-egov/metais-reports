// assets/dataset/metais_dup.js
import { GraphViewport } from '../common/viewport.js';

function ensureCssLoaded() {
  const id = 'metais-dup-css-link';
  if (document.getElementById(id)) return;

  const link = document.createElement('link');
  link.id = id;
  link.rel = 'stylesheet';
  link.href = 'assets/dataset/metais_dup.css';
  document.head.appendChild(link);
}

function layoutGroupAsNGon(group, radiusWorld) {
  const n = group.entities.length;
  if (n === 0) return;

  const angleStep = (2 * Math.PI) / n;
  const r = radiusWorld;

  group.entities.forEach((ent, i) => {
    const angle = i * angleStep;
    ent.x = r * Math.cos(angle);
    ent.y = r * Math.sin(angle);
  });
}

function buildSceneFromData(data) {
  const sceneEntities = data.entities || {};
  const groupsRaw     = data.groups   || [];
  const relsRaw       = data.relations || {};

  // Map uuid → { uuid, type, attrs }
  const entityMap = new Map();
  for (const [uuid, ent] of Object.entries(sceneEntities)) {
    entityMap.set(uuid, {
      uuid,
      type:  ent.type || 'UNKNOWN',
      attrs: ent.attributes || {},
    });
  }

  // For now: only first duplicity group
  if (!groupsRaw.length) {
    return { nodes: [], edges: [] };
  }

  const g0 = groupsRaw[0];
  const uuids0 = g0.entity_uuids || [];

  const groupEntities = uuids0
    .map(u => entityMap.get(u))
    .filter(Boolean);

  // Layout on a circle in world coords
  groupEntities.forEach(e => { e.x = 0; e.y = 0; });
  layoutGroupAsNGon({ entities: groupEntities }, 5.0);

  // Build nodes array
  const nodes = groupEntities.map(ent => ({
    id: ent.uuid,
    x:  ent.x,
    y:  ent.y,
    type: ent.type,
    attrs: ent.attrs,
  }));

  const inGroup = new Set(groupEntities.map(e => e.uuid));

  const edges = [];

  // 1) clique edges between all entities in the duplicity group
  for (let i = 0; i < groupEntities.length; i++) {
    for (let j = i + 1; j < groupEntities.length; j++) {
      edges.push({
        source: groupEntities[i].uuid,
        target: groupEntities[j].uuid,
        kind:   'duplicate',
      });
    }
  }

  // 2) relation edges between entities of this group
  for (const [relName, relInfo] of Object.entries(relsRaw)) {
    const pairs = relInfo.pairs || [];
    for (const [srcUUID, dstUUID] of pairs) {
      if (!inGroup.has(srcUUID) || !inGroup.has(dstUUID)) continue;
      edges.push({
        source: srcUUID,
        target: dstUUID,
        kind:   'relation',
        relName,
      });
    }
  }

  return { nodes, edges };
}

/**
 * @param {HTMLElement} container - where to render
 * @param {Object} data           - JSON loaded from data/<date>/dataset/metais_dup.json
 * @param {Object} ctx            - { date, category, instance, displayName }
 */
export function render(container, data, ctx) {
  ensureCssLoaded();
  container.innerHTML = '';

  const root = document.createElement('div');
  root.className = 'graph-root';
  container.appendChild(root);

  const scene = buildSceneFromData(data);

  const viewport = new GraphViewport(root, {
    debug: true,  // flip to false when you’re tired of the overlay
    getNodeSpriteType: node => node.type || 'UNKNOWN',
    getEdgeStyle: edge => {
      if (edge.kind === 'relation') {
        return { color: '#e4b84a', width: 1.5, arrow: true };
      } else {
        return { color: '#444', width: 1, arrow: false };
      }
    },
  });

  viewport.setScene(scene);
}