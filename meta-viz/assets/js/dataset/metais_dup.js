const GROUP_INDEX = 1;

const DUP_GLOW_RADIUS_FACTOR = 3.0;   // 2× icon radius
const DUP_GLOW_ALPHA         = 0.5;  // transparency
const DUP_GLOW_BLUR_TILES    = 0.9;   // blur in “tile units”

import {
  GraphViewport,
  attachHoverTooltip,
  attachSelectionBubble,
  attachEdgeHoverTooltip,
} from '../common/graph.js';

import {
  DARK_GRAPH_THEME,
  ICON_WORLD_SIZE,
  makeMetaisEdgeStyle,
  makeMetaisNodeScale,
} from '../common/graphStyles.js';

import { getNodeRadiusWorld } from '../common/spriteParams.js';
import { PhysicsSystem }      from '../common/entityPhysics.js';
import { DAMPING, REPULSION_DEFAULTS, CENTER_FORCE }            from '../common/physicsParams.js';

const DAMPING_LOW      = 0.1;   // glidy
const DAMPING_HIGH     = 1.0;   // settled
const DAMPING_DURATION = 10.0;  // seconds

const REPULSE_SCALE_LOW  = 0.0;  // no repulsion at start
const REPULSE_SCALE_HIGH = 5.0;  // full strength once settled

const CENTER_FORCE_LOW   = 0.0; // no pull at start
const CENTER_FORCE_HIGH  = 0.3;  // default

let dampingAnimT = 0;
let dampingAnimating = false;

function kickRelaxationRamp() {
  dampingAnimT = 0;
  dampingAnimating = true;

  // start glidy + soft repulsion
  DAMPING.gamma            = DAMPING_LOW;
  REPULSION_DEFAULTS.scale = REPULSE_SCALE_LOW;
  CENTER_FORCE.k           = CENTER_FORCE_LOW;
}

function ensureCssLoaded() {
  ensureCssLink('metais-graph-css-link',    'assets/css/graph.css');
  ensureCssLink('metais-graph-ui-css-link', 'assets/css/graph-ui.css');
  ensureCssLink('metais-dup-css-link',      'assets/css/metais_dup.css');
}

function ensureCssLink(id, href) {
  if (document.getElementById(id)) return;

  const link = document.createElement('link');
  link.id = id;
  link.rel = 'stylesheet';
  link.href = href;
  document.head.appendChild(link);
}

// normalize attributes so node.attrs is always an array
function normalizeAttributes(rawAttrs) {
  if (!rawAttrs) return [];
  if (Array.isArray(rawAttrs)) return rawAttrs;

  // Fallback for old format: { key: value } -> [{ attributeTechnicalName, attributeName, value }]
  return Object.entries(rawAttrs).map(([key, value]) => ({
    attributeTechnicalName: key,
    attributeName: key,
    value,
  }));
}

// Helper: extract display name from attrs list (Gen_Profil_nazov)
function getEntityNameFromAttrs(attrsArray) {
  if (!Array.isArray(attrsArray)) return '(bez názvu)';

  const nameAttr = attrsArray.find(
    a => a.attributeTechnicalName === 'Gen_Profil_nazov'
  );
  if (nameAttr && nameAttr.value != null && nameAttr.value !== '') {
    return String(nameAttr.value);
  }
  return '(bez názvu)';
}

function colorForGroupIndex(idx) {
  const hue = (idx * 77) % 360; // arbitrary step for nice spread
  return `hsl(${hue} 70% 50%)`; // we’ll tame opacity in the glow
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

/**
 * Build a scene for a single duplicity group, optionally shifted by (offsetX, offsetY)
 * in world coordinates so we can tile multiple groups in one viewport.
 */
function buildSceneFromData(data, groupIndex = 0, offsetX = 0, offsetY = 0, highlightColor = null) {
  const sceneEntities = data.entities || {};
  const groupsRaw     = data.groups   || [];
  const relsRaw       = data.relations || {};

  const entityMap = new Map();
  for (const [uuid, ent] of Object.entries(sceneEntities)) {
    entityMap.set(uuid, {
      uuid,
      type:  ent.type || 'UNKNOWN',
      attrs: normalizeAttributes(ent.attributes),
      x: 0,
      y: 0,
      isPrimary: false,
    });
  }

  if (!groupsRaw.length || !groupsRaw[groupIndex]) {
    return { nodes: [], edges: [] };
  }

  const g     = groupsRaw[groupIndex];
  const uuids = g.entity_uuids || [];

  const primaryEntities = uuids
    .map(u => entityMap.get(u))
    .filter(Boolean);

  const primarySet = new Set(primaryEntities.map(e => e.uuid));

  // Layout primaries on circle
  primaryEntities.forEach(e => { e.x = 0; e.y = 0; e.isPrimary = true; });
  const BASE_RADIUS = 5.0;
  layoutGroupAsNGon({ entities: primaryEntities }, BASE_RADIUS);

  const nodes   = [];
  const nodeMap = new Map();

  function ensureNode(uuid) {
    let node = nodeMap.get(uuid);
    if (node) return node;

    const ent = entityMap.get(uuid);
    if (!ent) return null;

    node = {
      id:    ent.uuid,
      x:     ent.x,
      y:     ent.y,
      type:  ent.type,
      attrs: ent.attrs,
      isPrimary: !!ent.isPrimary,
      highlightColor: null,
    };
    nodes.push(node);
    nodeMap.set(uuid, node);
    return node;
  }

  // Primaries first
  for (const ent of primaryEntities) {
    const node = ensureNode(ent.uuid);
    if (highlightColor && node) {
      node.highlightColor = highlightColor;
    }
  }

  const edges = [];

  // Clique edges between primaries
  for (let i = 0; i < primaryEntities.length; i++) {
    for (let j = i + 1; j < primaryEntities.length; j++) {
      edges.push({
        source: primaryEntities[i].uuid,
        target: primaryEntities[j].uuid,
        relName: "Has same MetaIS code",
        kind:   'duplicate',
      });
    }
  }

  // Neighbors tracking
  const neighborConnections = new Map();
  function addNeighborConnection(neighborUuid, primaryUuid) {
    let set = neighborConnections.get(neighborUuid);
    if (!set) {
      set = new Set();
      neighborConnections.set(neighborUuid, set);
    }
    set.add(primaryUuid);
  }

  // Relation edges where at least one endpoint is in the duplicity group
  for (const [relName, relInfo] of Object.entries(relsRaw)) {
    const pairs = relInfo.pairs || [];
    for (const [srcUUID, dstUUID] of pairs) {
      const srcIsPrimary = primarySet.has(srcUUID);
      const dstIsPrimary = primarySet.has(dstUUID);

      if (!srcIsPrimary && !dstIsPrimary) continue;

      const srcNode = ensureNode(srcUUID);
      const dstNode = ensureNode(dstUUID);
      if (!srcNode || !dstNode) continue;

      edges.push({
        source: srcUUID,
        target: dstUUID,
        kind:   'relation',
        relName,
      });

      if (srcIsPrimary && !dstIsPrimary) {
        addNeighborConnection(dstUUID, srcUUID);
      }
      if (dstIsPrimary && !srcIsPrimary) {
        addNeighborConnection(srcUUID, dstUUID);
      }
    }
  }

  const neighbors = nodes.filter(n => !n.isPrimary);

  const primaryAngles = new Map();
  for (const n of nodes) {
    if (n.isPrimary) {
      const angle = Math.atan2(n.y, n.x);
      primaryAngles.set(n.id, angle);
    }
  }

  let centerAssigned = false;

  if (neighbors.length === 1) {
    const nb = neighbors[0];
    const conns = neighborConnections.get(nb.id);
    if (conns && conns.size >= 2) {
      nb.x = 0;
      nb.y = 0;
      centerAssigned = true;
    }
  }

  const OUTER_RADIUS_BASE = BASE_RADIUS + 2.0;

  neighbors.forEach((nb, idx) => {
    if (centerAssigned && nb.x === 0 && nb.y === 0) {
      return;
    }

    const conns = neighborConnections.get(nb.id);
    let angle;

    if (conns && conns.size) {
      let sum = 0;
      let count = 0;
      for (const pid of conns) {
        const a = primaryAngles.get(pid);
        if (a != null) {
          sum += a;
          count += 1;
        }
      }
      angle = count > 0
        ? sum / count
        : (2 * Math.PI * idx) / Math.max(neighbors.length, 1);
    } else {
      angle = (2 * Math.PI * idx) / Math.max(neighbors.length, 1);
    }

    const radialOffset = 0.6 * (idx % 3);
    const radius = OUTER_RADIUS_BASE + radialOffset;

    nb.x = radius * Math.cos(angle);
    nb.y = radius * Math.sin(angle);
  });

  // apply offset so different groups can be tiled in world space
  if (offsetX !== 0 || offsetY !== 0) {
    for (const n of nodes) {
      n.x += offsetX;
      n.y += offsetY;
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

  const groups = data.groups || [];

  // ---------- header controls use more generally defined chips inside app-header ----------
  const appHeader = document.querySelector('.app-header');
  let headerControls = appHeader.querySelector('.app-header-controls');

  if (!headerControls) {
    headerControls = document.createElement('div');
    headerControls.className = 'app-header-controls';
    appHeader.appendChild(headerControls);
  }

  // Clear previous module controls when switching reports
  headerControls.innerHTML = '';

  // SECTION that owns the width (≈ 1/3 of header col2)
  const optionSection = document.createElement('div');
  optionSection.className = 'dup-header-option-section1';
  headerControls.appendChild(optionSection);
  
  const miscOptions = document.createElement('div');
  miscOptions.className = 'dup-header-option-section2';
  headerControls.appendChild(miscOptions);

  // Label
  const headerLabel = document.createElement('div');
  headerLabel.className = 'app-header-chip-label';
  headerLabel.textContent = 'Groups by Duplicate MetaIS Code:';
  optionSection.appendChild(headerLabel);

  // Shell that will handle the hover / unroll
  const chipShell = document.createElement('div');
  chipShell.className = 'app-header-chip-shell';
  optionSection.appendChild(chipShell);

  // Actual chip container that JS fills with buttons
  const chipContainer = document.createElement('div');
  chipContainer.className = 'app-header-chip-container';
  chipShell.appendChild(chipContainer);

  // ---------- GRAPH PANEL ----------
  const graphPanel = container;
  graphPanel.classList.add('graph-panel');

  // Main row: graph
  const mainRow = document.createElement('div');
  mainRow.className = 'graph-panel-main';
  graphPanel.appendChild(mainRow);

  const root = document.createElement('div');
  root.className = 'graph-root';
  mainRow.appendChild(root);

  // Viewport setup
  const getEdgeStyle = makeMetaisEdgeStyle();
  const getNodeScale = makeMetaisNodeScale();

  const viewport = new GraphViewport(root, {
    debug: false,
    ...DARK_GRAPH_THEME,
    iconWorldSize: ICON_WORLD_SIZE,
    getNodeSpriteType: node => node.type || 'UNKNOWN',
    getNodeScale,
    getEdgeStyle,

    getNodeGlow: (node) => {
      if (!node.highlightColor) return null;
      return {
        color: node.highlightColor,
        radiusFactor: DUP_GLOW_RADIUS_FACTOR,
        alpha: DUP_GLOW_ALPHA,
        blurTiles: DUP_GLOW_BLUR_TILES,
      };
    },
  });

  const physics = new PhysicsSystem({
    timeScale: 1.0,
    maxDt: 0.03,
    isSpringEdge: (edge) =>
        edge.kind === 'relation' || edge.kind === 'duplicate',
  });

  // Remember last scene & selection so we can keep positions
  let lastScene = { nodes: [], edges: [] };
  let lastSelectedGroups = new Set();

  // ---------- Multi-group scene builder ----------
  function buildSceneForGroups(selectedIndices) {
    if (!selectedIndices.size) {
      return { nodes: [], edges: [] };
    }

    const allNodes = [];
    const allEdges = [];
    const globalNodeMap = new Map(); // uuid -> node

    const indices = Array.from(selectedIndices).sort((a, b) => a - b);

    // Previous positions: id -> { x, y }
    const prevPos = new Map();
    for (const n of lastScene.nodes || []) {
      prevPos.set(n.id, { x: n.x, y: n.y });
    }

    // Find how far the existing cloud extends, so we can spawn new groups outside it
    let maxR = 0;
    for (const p of prevPos.values()) {
      const r = Math.hypot(p.x, p.y);
      if (r > maxR) maxR = r;
    }
    const BASE_SPAWN_RADIUS = 0;  // world units outside current cloud
    const SPAWN_RADIUS_STEP = 2;         // extra per-group spread

    indices.forEach((groupIdx, i) => {
      const isNewGroup = !lastSelectedGroups.has(groupIdx);

      // Spawn angle for this group (golden-angle-ish spread)
      const angle = groupIdx * 2.399963229728653; // ≈ 137.5°
      const spawnRadius = isNewGroup ? (BASE_SPAWN_RADIUS + i * SPAWN_RADIUS_STEP) : 0;

      const offsetX = isNewGroup ? spawnRadius * Math.cos(angle) : 0;
      const offsetY = isNewGroup ? spawnRadius * Math.sin(angle) : 0;

      // Initial layout for this group (only really used for *new* nodes)
      const groupColor = colorForGroupIndex(groupIdx);

      // Initial layout for this group (only really used for *new* nodes)
      const { nodes, edges } = buildSceneFromData(
        data,
        groupIdx,
        offsetX,
        offsetY,
        groupColor,
      );

      for (const n of nodes) {
        const prev = prevPos.get(n.id);

        // Reuse old position if exists
        if (prev) {
          n.x = prev.x;
          n.y = prev.y;
        }

        const existing = globalNodeMap.get(n.id);

        if (!existing) {
          // Brand-new node in global scene
          globalNodeMap.set(n.id, n);
          allNodes.push(n);
        } else {
          // Node already exists in global map -> ONLY update highlight
          // Preserve existing positions (we already restored n.x,n.y)
          if (!existing.highlightColor && n.highlightColor) {
            existing.highlightColor = n.highlightColor;
          }
        }
      }

      allEdges.push(...edges);
    });

    return { nodes: allNodes, edges: allEdges };
  }

  function updateScene(selectedIndices) {
    const scene = buildSceneForGroups(selectedIndices);
    viewport.setScene(scene);
    physics.setGraph(scene.nodes, scene.edges);

    lastScene = scene;
    lastSelectedGroups = new Set(selectedIndices);
  }

  // ---------- Hover tooltip (type: name + uuid) ----------
  attachHoverTooltip(viewport, graphPanel, {
    renderTooltip: (node, el) => {
      const attrs = Array.isArray(node.attrs) ? node.attrs : [];
      const name  = getEntityNameFromAttrs(attrs);
      const uuid  = node.id;

      el.innerHTML = '';

      const titleEl = document.createElement('div');
      titleEl.className = 'graph-tooltip-title';
      titleEl.textContent = `${node.type || 'UNKNOWN'}: ${name}`;

      const idEl = document.createElement('div');
      idEl.className = 'graph-tooltip-id';
      idEl.textContent = uuid;

      el.appendChild(titleEl);
      el.appendChild(idEl);
    },
  });

  attachEdgeHoverTooltip(viewport, graphPanel, {
    renderTooltip: (edge, el) => {
      el.innerHTML = '';

      if (edge.kind === 'duplicate') {
        const srcEnt = data.entities?.[edge.source] || {};
        const dstEnt = data.entities?.[edge.target] || {};

        const srcType = srcEnt.type || '?';
        const dstType = dstEnt.type || '?';

        const title = document.createElement('div');
        title.className = 'graph-edge-title';
        title.textContent = edge.relName || 'Has same MetaIS code';

        const types = document.createElement('div');
        types.className = 'graph-edge-types';
        types.textContent = `${srcType} ↔ ${dstType}`;

        el.appendChild(title);
        el.appendChild(types);
        return;
      }

      const relName = edge.relName || '(neznámy vzťah)';
      const relInfo = data.relations?.[edge.relName] || {};
      const srcType = relInfo.source_type || '?';
      const tgtType = relInfo.target_type || '?';

      const title = document.createElement('div');
      title.className = 'graph-edge-title';
      title.textContent = relName;

      const types = document.createElement('div');
      types.className = 'graph-edge-types';
      types.textContent = `${srcType} -> ${tgtType}`;

      el.appendChild(title);
      el.appendChild(types);
    },
  });

  // ---------- Persistent details bubble under node ----------
  attachSelectionBubble(viewport, graphPanel, {
    radiusFactor: 0.5,
    renderBubble: (node, bubbleEl) => {
      const attrs = Array.isArray(node.attrs) ? node.attrs : [];
      const name  = getEntityNameFromAttrs(attrs);

      const title = document.createElement('div');
      title.className = 'graph-selected-title';
      title.textContent = `${node.type || 'UNKNOWN'}: ${name}`;

      const idRow = document.createElement('div');
      idRow.className = 'graph-selected-id';
      idRow.textContent = node.id;

      bubbleEl.appendChild(title);
      bubbleEl.appendChild(idRow);

      const attrsWrap = document.createElement('div');
      attrsWrap.className = 'graph-selected-attrs';

      attrs.forEach(attr => {
        const tech  = attr.attributeTechnicalName;
        const label = attr.attributeName || tech;
        const value = attr.value;

        if (tech === 'Gen_Profil_nazov') return;
        if (value === null || value === undefined || value === '') return;

        const row = document.createElement('div');
        row.className = 'graph-selected-row';

        const keyEl = document.createElement('span');
        keyEl.className = 'graph-selected-key';
        keyEl.textContent = String(label);

        const sepEl = document.createElement('span');
        sepEl.className = 'graph-selected-sep';
        sepEl.textContent = ': ';

        const valEl = document.createElement('span');
        valEl.className = 'graph-selected-val';
        valEl.textContent = String(value);

        row.appendChild(keyEl);
        row.appendChild(sepEl);
        row.appendChild(valEl);
        attrsWrap.appendChild(row);
      });

      bubbleEl.appendChild(attrsWrap);
    },
  });

  // ---------- CHIP UI LOGIC (multi-select groups) ----------
  function getGroupLabel(g, idx) {
    const code =
      g.metais_code ||
      (Array.isArray(g.metais_codes) && g.metais_codes.join(', ')) ||
      g.code ||
      null;

    const count = g.entity_uuids ? g.entity_uuids.length : 0;
    return code ? `${code} (${count})` : `Group ${idx} (${count})`;
  }

  const selectedGroups = new Set();
  if (groups.length > 0) {
    selectedGroups.add(0); // show first group by default
  }

  function refreshChips() {
    chipContainer.innerHTML = '';

    groups.forEach((g, idx) => {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'app-header-chip';
      chip.dataset.index = String(idx);
      chip.textContent = getGroupLabel(g, idx);

      if (selectedGroups.has(idx)) {
        chip.classList.add('app-header-chip-active');
      }

      chip.addEventListener('click', () => {
        if (selectedGroups.has(idx)) {
          selectedGroups.delete(idx);
        } else {
          selectedGroups.add(idx);
        }
        refreshChips();
        updateScene(selectedGroups);
        kickRelaxationRamp();
      });

      chipContainer.appendChild(chip);
    });
  }

  const selectAllBtn = document.createElement('button');
  selectAllBtn.type = 'button';
  selectAllBtn.className = 'app-header-chip app-header-chip-utility';
  selectAllBtn.textContent = 'Select all groups';

  selectAllBtn.addEventListener('click', () => {
    selectedGroups.clear();
    groups.forEach((_, idx) => selectedGroups.add(idx));
    refreshChips();
    updateScene(selectedGroups);
    kickRelaxationRamp();
  });

  miscOptions.appendChild(selectAllBtn);

  refreshChips();
  updateScene(selectedGroups);
  kickRelaxationRamp();

  // --- Animation loop: physics + redraw ---
  let lastTime = performance.now();

  function tick(now) {
    const dt = (now - lastTime) / 1000;
    lastTime = now;

    if (dampingAnimating) {
      dampingAnimT += dt;
      const t = Math.min(dampingAnimT / DAMPING_DURATION, 1.0);

      // ease-out for nicer feel
      const eased = 1 - (1 - t) * (1 - t);

      DAMPING.gamma = DAMPING_LOW + (DAMPING_HIGH - DAMPING_LOW) * eased;
      REPULSION_DEFAULTS.scale = REPULSE_SCALE_LOW + (REPULSE_SCALE_HIGH - REPULSE_SCALE_LOW) * eased;
      CENTER_FORCE.k = CENTER_FORCE_LOW + (CENTER_FORCE_HIGH - CENTER_FORCE_LOW) * eased;

      if (t >= 1.0) {
        dampingAnimating         = false;
        DAMPING.gamma            = DAMPING_HIGH;
        REPULSION_DEFAULTS.scale = REPULSE_SCALE_HIGH;
        CENTER_FORCE.k           = CENTER_FORCE_HIGH;
      }
    }

    physics.step(dt);
    viewport.draw();

    requestAnimationFrame(tick);
    //console.log('gamma =', DAMPING.gamma.toFixed(2));
  }

  requestAnimationFrame(tick);
}