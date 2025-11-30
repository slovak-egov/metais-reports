const DUP_GLOW_RADIUS_FACTOR = 3.0;
const DUP_GLOW_ALPHA         = 0.5;
const DUP_GLOW_BLUR_TILES    = 0.9;
const HUB_HIGHLIGHT_COLOR    = '#ffffff7c';

import { buildRelationEdges, buildDuplicateCliqueEdges, buildAdjacency, collectNeighborsWithin, buildSceneForNodeSetBase } from '../common/graphOps.js';

import { renderControlsRow } from '../common/controlsRenderer.js';

import { createPillMenu } from '../common/pillMenu.js';

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

import { PhysicsSystem } from '../common/entityPhysics.js';
import {
  REPULSION_DEFAULTS,
  CENTER_FORCE,
  SPRING_DEFAULTS,
  DAMPING,
  SPRING_DAMPING,
} from '../common/physicsParams.js';

import { attachPhysicsPanel } from '../common/physicsPanel.js';

import { showContextMenu, closeContextMenu } from '../common/contextMenu.js';

function ensureCssLoaded() {
  ensureCssLink('metais-graph-css-link',    'assets/css/graph.css');
  ensureCssLink('metais-graph-ui-css-link', 'assets/css/graph-ui.css');
  // ensureCssLink('metais-dup-css-link',      'assets/css/metais_dup.css');
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

// Deterministic but “random-looking” color by group index
function colorForGroupIndex(idx) {
  const hue = (idx * 77) % 360; // arbitrary step for nice spread
  return `hsl(${hue} 70% 50%)`;
}

/**
 * @param {HTMLElement} container - where to render
 * @param {Object} data           - JSON loaded from data/<date>/dataset/metais_dup.json
 * @param {Object} ctx            - { date, category, instance, displayName }
 */
export function render(container, data, ctx) {
  // Make this report use the full height (hide header row)
  // document.body.classList.add('metais-dup-collapsed-header');

  // ---- PRECOMPUTED STRUCTURES ----
  const groups   = data.groups   || [];
  const orphans  = data.orphans  || []; // [{group_index, metais_code, count, uuids}]
  const hubsData = data.hubs     || []; // [{hub_uuid, count, layers:[{count,uuids:[]}, ...]}]
  const islands  = data.islands  || []; // [{count, uuids:[hub_uuid,...]}]
  const entities = data.entities || {};
  const relsRaw  = data.relations || {};

  const primaryGroupIndexByUuid = new Map();
  groups.forEach((g, idx) => {
    (g.entity_uuids || []).forEach(u => {
      if (!primaryGroupIndexByUuid.has(u)) {
        primaryGroupIndexByUuid.set(u, idx);
      }
    });
  });

  const hubById = new Map(hubsData.map(h => [h.hub_uuid, h]));

  const orphanGroupIndices = orphans
    .map(o => o.group_index)
    .filter(idx => typeof idx === 'number' && idx >= 0 && idx < groups.length);

  // which uuids are “duplicity primaries”
  const duplicatedPrimaries = new Set();
  groups.forEach(g => {
    (g.entity_uuids || []).forEach(u => duplicatedPrimaries.add(u));
  });

  // baseNodes: everything we know about each entity (no selection logic)
  const baseNodes = new Map(); // uuid -> {id,type,attrs,isPrimary,isInvalidated}
  for (const [uuid, ent] of Object.entries(entities)) {
    const meta          = ent.metaAttributes || {};
    const isInvalidated = meta.state === 'INVALIDATED';
    const isPrimary     = duplicatedPrimaries.has(uuid);

    baseNodes.set(uuid, {
      id:    uuid,
      type:  ent.type || 'UNKNOWN',
      attrs: normalizeAttributes(ent.attributes),
      isPrimary,
      isInvalidated,
    });
  }

  const allRelationEdges  = buildRelationEdges(relsRaw);
  const allDuplicateEdges = buildDuplicateCliqueEdges(groups);
  const adjacency         = buildAdjacency(allRelationEdges);

  function showDupContextMenu(node, event) {
    const items = [];

    items.push({
      label: 'Limit selection to this element',
      onClick: () => {
        //console.log('[Dup] menu: limit to element', node.id);
        limitSelectionTo(node.id, 0);
      },
    });

    [0, 1, 2, 3, Infinity].forEach(dist => {
      const label =
        dist === Infinity
          ? 'Limit selection: element and all related'
          : `Limit selection: element and related up to ${dist}`;
      items.push({
        label,
        onClick: () => {
          //console.log('[Dup] menu: limit selection around', { id: node.id, dist });
          limitSelectionTo(node.id, dist);
        },
      });
    });

    items.push({ type: 'separator' });

    [0, 1, 2, 3, Infinity].forEach(dist => {
      const label =
        dist === Infinity
          ? 'Remove: element and all related'
          : `Remove: element and related up to ${dist}`;
      items.push({
        label,
        onClick: () => {
          //console.log('[Dup] menu: remove selection around', { id: node.id, dist });
          removeSelectionAround(node.id, dist);
        },
      });
    });

    // ⬇ important: pass the real PointerEvent
    showContextMenu(graphPanel, event, items);
  }

  const maxHubLayers = hubsData.reduce(
    (m, h) => Math.max(m, (h.layers || []).length),
    0
  );

  // ---- VIEW / SELECTION STATE ----
  const state = {
    mode: 'groups',             // 'groups' | 'hubs' | 'islands'
    selectedGroups:  new Set(),
    selectedHubs:    new Set(),
    selectedIslands: new Set(),
    includeOrphans:  false,
    hubLayerDepth:   Infinity,
    maxRelationDistance: Infinity,
    focusSet: null,
  };

  // we show first group by default if available
  if (groups.length > 0) {
    state.selectedGroups.add(0);
  }

  let lastScene = { nodes: [], edges: [] };
  let lastSelectedGroups = new Set();

  // chip containers will be assigned after createChipSection
  let groupChipContainer   = null;
  let hubChipContainer     = null;
  let islandChipContainer  = null;

  const selectAllTimers = [];

  function cancelSelectAllTimers() {
    while (selectAllTimers.length) {
      const id = selectAllTimers.pop();
      clearTimeout(id);
    }
  }

  // ---- SCENE BUILDERS ----

  function buildSceneForNodeSet(uuidSet, { highlightHubs = false, groupColorByUuid = null } = {}) {
    const baseScene = buildSceneForNodeSetBase({
      uuidSet,
      baseNodes,
      allEdges: [...allRelationEdges, ...allDuplicateEdges],
      prevScene: lastScene,
    });

    for (const node of baseScene.nodes) {
      const base = baseNodes.get(node.id);
      const isHub     = hubById.has(node.id);
      const isPrimary = base.isPrimary;

      let highlightColor = null;

      if (groupColorByUuid && groupColorByUuid.has(node.id)) {
        highlightColor = groupColorByUuid.get(node.id);
      } else if (highlightHubs && isHub) {
        highlightColor = HUB_HIGHLIGHT_COLOR;
      } else if (isPrimary) {
        highlightColor = '#888888';
      }
      node.highlightColor = highlightColor;
    }

    // relation distance filter is also MetaIS-specific
    const filteredEdges = baseScene.edges.filter(e => {
      if (e.kind !== 'relation') return true;
      if (state.maxRelationDistance === Infinity) return true;
      const d = e.distance;
      return typeof d === 'number' && d >= 0 && d <= state.maxRelationDistance;
    });

    return { nodes: baseScene.nodes, edges: filteredEdges };
  }

  // GROUPS MODE: primaries + neighbors, with per-group colors
  function buildSceneForGroups(selectedIndices) {
    if (!selectedIndices.size) return { nodes: [], edges: [] };

    const primarySet = new Set();
    const groupColorByUuid = new Map();

    // assign a deterministic hue per group, then to that group's primaries
    selectedIndices.forEach(idx => {
      const g = groups[idx];
      if (!g) return;

      const color = colorForGroupIndex(idx);
      (g.entity_uuids || []).forEach(u => {
        primarySet.add(u);
        if (!groupColorByUuid.has(u)) {
          groupColorByUuid.set(u, color);
        }
      });
    });

    // neighbors = any node that shares an edge with a primary
    const uuidSet = new Set(primarySet);
    for (const e of allRelationEdges) {
      if (primarySet.has(e.source)) uuidSet.add(e.target);
      if (primarySet.has(e.target)) uuidSet.add(e.source);
    }

    // duplicate edges use allDuplicateEdges; no need to special-case here
    return buildSceneForNodeSet(uuidSet, {
      highlightHubs: false,
      groupColorByUuid,
    });
  }

  // HUBS / ISLANDS MODE: hubs + layers (+ optional orphans)
  function buildSceneForHubsAndIslands() {
    const uuidSet = new Set();

    // hubs + their layers
    state.selectedHubs.forEach(hid => {
      const hub = hubById.get(hid);
      if (!hub) return;

      uuidSet.add(hid);

      const layers = hub.layers || [];
      if (!layers.length) return;

      const maxIdx = (state.hubLayerDepth === Infinity)
        ? layers.length - 1
        : Math.min(state.hubLayerDepth - 1, layers.length - 1);

      for (let li = 0; li <= maxIdx; li++) {
        (layers[li].uuids || []).forEach(u => uuidSet.add(u));
      }
    });

    // orphans
    if (state.includeOrphans) {
      orphans.forEach(o => {
        (o.uuids || []).forEach(u => uuidSet.add(u));
      });
    }

    if (!uuidSet.size) return { nodes: [], edges: [] };

    // color primaries by their duplicity group even in hubs/islands
    const groupColorByUuid = new Map();
    uuidSet.forEach(u => {
      if (!duplicatedPrimaries.has(u)) return;
      const gIdx = primaryGroupIndexByUuid.get(u);
      if (gIdx == null) return;
      groupColorByUuid.set(u, colorForGroupIndex(gIdx));
    });

    return buildSceneForNodeSet(uuidSet, {
      highlightHubs: true,
      groupColorByUuid,
    });
  }

  function updateScene() {
    if (!viewport || !physics) return;

    let scene;

    if (state.focusSet && state.focusSet.size) {
      // explicit node subset
      scene = buildSceneForNodeSet(state.focusSet, {
        highlightHubs: true,
      });
    } else if (state.mode === 'groups') {
      scene = buildSceneForGroups(state.selectedGroups);
    } else {
      scene = buildSceneForHubsAndIslands();
    }

    viewport.setScene(scene);
    physics.setGraph(scene.nodes, scene.edges);
    lastScene = scene;
  }

  function limitSelectionTo(nodeId, maxDist) {
    //console.log('[Dup] limitSelectionTo()', { nodeId, maxDist });
    //console.time('[Dup] collectNeighbors(limit)');
    const nb = collectNeighborsWithin(adjacency, nodeId, maxDist);
    //console.timeEnd('[Dup] collectNeighbors(limit)');
    //console.log('[Dup] focusSet size', nb.size);

    state.focusSet = nb;
    updateScene();
  }

  function removeSelectionAround(nodeId, maxDist) {
    //console.log('[Dup] removeSelectionAround()', { nodeId, maxDist });

    if (!state.focusSet) {
      const baseSet = new Set();
      (lastScene.nodes || []).forEach(n => baseSet.add(n.id));
      state.focusSet = baseSet;
      //console.log('[Dup] initialized focusSet from lastScene, size', state.focusSet.size);
    }

    //console.time('[Dup] collectNeighbors(remove)');
    const toRemove = collectNeighborsWithin(adjacency, nodeId, maxDist);
    //console.timeEnd('[Dup] collectNeighbors(remove)');
    //console.log('[Dup] toRemove size', toRemove.size);

    toRemove.forEach(id => state.focusSet.delete(id));
    //console.log('[Dup] focusSet size after removal', state.focusSet.size);

    updateScene();
  }

  // ---------- CHIP UI LOGIC (groups / hubs / islands) ----------
  function getGroupLabel(g, idx) {
    const code =
      g.metais_code ||
      (Array.isArray(g.metais_codes) && g.metais_codes.join(', ')) ||
      g.code ||
      null;

    const count = g.entity_uuids ? g.entity_uuids.length : 0;
    return code ? `${code} (${count})` : `Group ${idx} (${count})`;
  }

  function renderGroupChips() {
    if (!groupChipContainer) return;
    groupChipContainer.innerHTML = '';

    groups.forEach((g, idx) => {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'app-header-chip';
      chip.dataset.index = String(idx);
      chip.textContent = getGroupLabel(g, idx);

      if (state.selectedGroups.has(idx)) {
        chip.classList.add('app-header-chip-active');
      }

      chip.addEventListener('click', () => {
        // switch to groups mode
        if (state.mode !== 'groups') {
          state.mode = 'groups';
          state.selectedHubs.clear();
          state.selectedIslands.clear();
          renderHubChips();
          renderIslandChips();
        }

        // user took manual control -> stop any running select-all animation
        cancelSelectAllTimers();

        if (state.selectedGroups.has(idx)) {
          state.selectedGroups.delete(idx);
        } else {
          state.selectedGroups.add(idx);
        }

        // maintain orphan inclusion if enabled
        if (state.includeOrphans) {
          orphanGroupIndices.forEach(oid => state.selectedGroups.add(oid));
        }

        renderGroupChips();
        updateScene();
      });

      groupChipContainer.appendChild(chip);
    });
  }

  function renderHubChips() {
    if (!hubChipContainer) return;
    hubChipContainer.innerHTML = '';

    hubsData.forEach((hub) => {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'app-header-chip';
      chip.textContent = `${hub.hub_uuid} (${hub.count})`;

      if (state.selectedHubs.has(hub.hub_uuid)) {
        chip.classList.add('app-header-chip-active');
      }

      chip.addEventListener('click', () => {
        // switch to hubs mode
        if (state.mode !== 'hubs' && state.mode !== 'islands') {
          state.mode = 'hubs';
          state.selectedGroups.clear();
          state.selectedIslands.clear();
          renderGroupChips();
          renderIslandChips();
        }

        if (state.selectedHubs.has(hub.hub_uuid)) {
          state.selectedHubs.delete(hub.hub_uuid);
        } else {
          state.selectedHubs.add(hub.hub_uuid);
        }

        renderHubChips();
        updateScene();
      });

      hubChipContainer.appendChild(chip);
    });
  }

  function renderIslandChips() {
    if (!islandChipContainer) return;
    islandChipContainer.innerHTML = '';

    islands.forEach((island, idx) => {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'app-header-chip';

      const hubsCount = (island.uuids || []).length;
      chip.textContent = `Island ${idx + 1} (${island.count} nodes, ${hubsCount} hubs)`;

      if (state.selectedIslands.has(idx)) {
        chip.classList.add('app-header-chip-active');
      }

      chip.addEventListener('click', () => {
        // switch to islands mode
        state.mode = 'islands';
        state.selectedGroups.clear();
        renderGroupChips();

        if (state.selectedIslands.has(idx)) {
          state.selectedIslands.delete(idx);
        } else {
          state.selectedIslands.add(idx);
        }

        // hubs in selected islands
        state.selectedHubs.clear();
        state.selectedIslands.forEach(i => {
          (islands[i]?.uuids || []).forEach(hid => state.selectedHubs.add(hid));
        });

        // show all layers for islands by default
        state.hubLayerDepth = Infinity;

        renderIslandChips();
        renderHubChips();
        updateScene();
      });

      islandChipContainer.appendChild(chip);
    });
  }

  // Gradual "select all": largest groups first
  function selectAllGradual() {
    cancelSelectAllTimers();

    // start from empty selection so animation is visually obvious
    state.selectedGroups.clear();

    // keep orphans selection consistent with includeOrphans
    if (state.includeOrphans) {
      orphanGroupIndices.forEach(idx => state.selectedGroups.add(idx));
    }

    renderGroupChips();
    updateScene();

    // order groups by descending size
    const ordered = groups
      .map((g, idx) => ({
        idx,
        size: (g.entity_uuids ? g.entity_uuids.length : 0),
      }))
      .sort((a, b) => b.size - a.size)
      .map(g => g.idx);

    const STEP_MS = 50;

    ordered.forEach((idx, step) => {
      const handle = setTimeout(() => {
        state.selectedGroups.add(idx);

        if (state.includeOrphans) {
          orphanGroupIndices.forEach(oid => state.selectedGroups.add(oid));
        }

        renderGroupChips();
        updateScene();
      }, step * STEP_MS);

      selectAllTimers.push(handle);
    });
  }

  // ---------- CONTROL CONFIGS ----------

  const GROUPS_CONTROLS = [
    {
      type: 'toggle',
      id: 'orphans',
      labelOn:  'Exclude orphans',
      labelOff: 'Include orphans',
      get: () => state.includeOrphans,
      set: (val) => {
        state.includeOrphans = val;

        if (state.mode === 'groups') {
          if (val) {
            orphanGroupIndices.forEach(idx => state.selectedGroups.add(idx));
          } else {
            orphanGroupIndices.forEach(idx => state.selectedGroups.delete(idx));
          }
          renderGroupChips();
        }
        updateScene();
      },
    },
  ];

  const HUBS_CONTROLS = [];

  const ISLAND_CONTROLS = [];

  const GLOBAL_CONTROLS = [
    {
      type: 'button',
      id: 'clearAll',
      label: 'Clear',
      onClick: () => {
        cancelSelectAllTimers();
        state.selectedGroups.clear();
        state.selectedHubs.clear();
        state.selectedIslands.clear();

        renderGroupChips();
        renderHubChips();
        renderIslandChips();
        updateScene();
      },
    },
    {
      type: 'button',
      id: 'selectAll',
      label: 'Select all',
      onClick: () => {
        // “Select all” means: go to groups mode and gradually select all groups
        if (state.mode !== 'groups') {
          state.mode = 'groups';
          state.selectedHubs.clear();
          state.selectedIslands.clear();
          renderHubChips();
          renderIslandChips();
        }
        selectAllGradual();
      },
    },
    {
      type: 'chipGroup',
      id: 'relationDistance',
      label: 'Relations up to:',
      items: () => [0, 1, 2, 3, 'All'],
      isActive: (v) =>
        v === 'All'
          ? state.maxRelationDistance === Infinity
          : state.maxRelationDistance === v,
      onSelect: (v) => {
        state.maxRelationDistance = (v === 'All' ? Infinity : v);
        updateScene();
      },
    },
    {
      type: 'chipGroup',
      id: 'hubLayers',
      label: 'Layers:',
      items: () => {
        if (maxHubLayers <= 0) return ['All'];
        const arr = [];
        for (let d = 1; d <= maxHubLayers; d++) arr.push(`L${d}`);
        arr.push('All');
        return arr;
      },
      isActive: (v) => {
        if (v === 'All') return state.hubLayerDepth === Infinity;
        const depth = Number(v.slice(1)); // "L3" -> 3
        return state.hubLayerDepth === depth;
      },
      onSelect: (v) => {
        state.hubLayerDepth = (v === 'All' ? Infinity : Number(v.slice(1)));
        if (state.mode === 'hubs' || state.mode === 'islands' || state.mode === 'groups') {
          updateScene();
        }
      },
    },
  ];

  // ---- DOM + UI ----

  ensureCssLoaded();
  container.innerHTML = '';

  const graphPanel = container;
  graphPanel.classList.add('graph-panel');

  // --- Floating pill bar: Groups / Hubs / Islands ---
  const { bar: pillBar } = createPillMenu(graphPanel, [
    {
      id: 'groups',
      label: 'Groups',
      build: ({ controlsEl, chipsEl }) => {
        // wire controls + chip container
        renderControlsRow(GROUPS_CONTROLS, controlsEl);
        groupChipContainer = chipsEl;
      },
    },
    {
      id: 'hubs',
      label: 'Hubs',
      build: ({ controlsEl, chipsEl }) => {
        renderControlsRow(HUBS_CONTROLS, controlsEl);
        hubChipContainer = chipsEl;
      },
    },
    {
      id: 'islands',
      label: 'Islands',
      build: ({ controlsEl, chipsEl }) => {
        renderControlsRow(ISLAND_CONTROLS, controlsEl);
        islandChipContainer = chipsEl;
      },
    },
  ]);

  // Global controls row to the right of the pill buttons
  const globalControlsEl = document.createElement('div');
  globalControlsEl.className = 'dup-pill-global-controls';
  pillBar.appendChild(globalControlsEl);
  renderControlsRow(GLOBAL_CONTROLS, globalControlsEl);

  // Main graph row + canvas
  const mainRow = document.createElement('div');
  mainRow.className = 'graph-panel-main';
  graphPanel.appendChild(mainRow);

  const root = document.createElement('div');
  root.className = 'graph-root';
  mainRow.appendChild(root);

  // ... pills ...

  const getEdgeStyle   = makeMetaisEdgeStyle();
  const rawGetNodeScale = makeMetaisNodeScale();

  const getNodeScale = (node) => {
    let s = rawGetNodeScale(node);
    if (!Number.isFinite(s) || s <= 0) {
      console.warn('Bad node scale, falling back to 1:', s, node);
      s = 1;
    }
    return s;
  };

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
        color:        node.highlightColor,
        radiusFactor: DUP_GLOW_RADIUS_FACTOR,
        alpha:        DUP_GLOW_ALPHA,
        blurTiles:    DUP_GLOW_BLUR_TILES,
      };
    },

    // grayscale invalidated nodes
    getNodeSpriteStyle: (node) => {
      if (node.isInvalidated) {
        return {
          grayscale: true,
          alpha: 0.6,
        };
      }
      return null;
    },

    onNodeContextMenu: (node, evt) => {
      //console.log('[Dup] onNodeContextMenu fired', node && node.id, evt);
      if (!node || !evt) return;
      showDupContextMenu(node, evt);
    },
  });

  const physics = new PhysicsSystem({
    timeScale: 1.0,
    maxDt: 0.03,
    isSpringEdge: (edge) =>
      edge.kind === 'relation' || edge.kind === 'duplicate',
  });

  attachPhysicsPanel({ parent: graphPanel, physics });

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

      // duplicate edges
      if (edge.kind === 'duplicate') {
        const srcEnt = data.entities?.[edge.source] || {};
        const dstEnt = data.entities?.[edge.target] || {};

        const srcType = srcEnt.type || 'Any';
        const dstType = dstEnt.type || 'Any';

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

      // normal relations
      const relName = edge.relName || '(neznámy vzťah)';
      const relInfo = data.relations?.[edge.relName] || {};

      const srcEnt = data.entities?.[edge.source] || {};
      const dstEnt = data.entities?.[edge.target] || {};

      const srcType =
        relInfo.source_type ||
        srcEnt.type ||
        'Any';

      const tgtType =
        relInfo.target_type ||
        dstEnt.type ||
        'Any';

      const title = document.createElement('div');
      title.className = 'graph-edge-title';
      title.textContent = relName;

      const types = document.createElement('div');
      types.className = 'graph-edge-types';
      types.textContent = `${srcType} → ${tgtType}`;

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

  // ---------- initial UI + scene ----------
  renderGroupChips();
  renderHubChips();
  renderIslandChips();
  updateScene();

  // --- Animation loop: physics + redraw ---
  let lastTime = performance.now();

  function tick(now) {
    const dt = (now - lastTime) / 1000;
    lastTime = now;

    physics.step(dt);
    viewport.draw();

    requestAnimationFrame(tick);
  }

  requestAnimationFrame(tick);
}