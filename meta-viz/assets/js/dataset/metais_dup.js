const DUP_GLOW_RADIUS_FACTOR = 3.0;
const DUP_GLOW_ALPHA         = 0.7;
const DUP_GLOW_BLUR_TILES    = 0.9;

import {
  buildRelationEdges,
  buildAdjacency,
  collectNeighborsWithin,
  buildSceneForNodeSetBase,
} from '../common/graphOps.js';

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
import { attachPhysicsPanel } from '../common/physicsPanel.js';
import { showContextMenu } from '../common/contextMenu.js';

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
  // ---- PRECOMPUTED STRUCTURES ----
  const groups   = data.groups   || [];
  const orphans  = data.orphans  || [];
  const poView   = data.po_view  || [];
  const islandsRaw  = data.islands  || [];
  const entities = data.entities || {};
  const relsRaw  = data.relations || {};

  const islandsByLevel = Array.isArray(islandsRaw)
    ? { '0': islandsRaw }
    : islandsRaw;

  // numeric distance keys we actually have (e.g. ["0","1","2","3"] -> [0,1,2,3])
  const numericDistanceKeys = Object.keys(islandsByLevel)
    .filter(k => !Number.isNaN(Number(k)))
    .map(k => Number(k))
    .sort((a, b) => a - b);

  const availableDistances = numericDistanceKeys.length ? numericDistanceKeys : [0];
  const maxAvailableDistance =
    availableDistances[availableDistances.length - 1];

  function getIslandsForCurrentDistance() {
    const key = String(state.maxRelationDistance);
    return islandsByLevel[key] || [];
  }

  const primaryGroupIndexByUuid = new Map();
  groups.forEach((g, idx) => {
    (g.entity_uuids || []).forEach(u => {
      if (!primaryGroupIndexByUuid.has(u)) {
        primaryGroupIndexByUuid.set(u, idx);
      }
    });
  });

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
  const adjacency         = buildAdjacency(allRelationEdges);

  // ---- VIEW / SELECTION STATE ----
  const state = {
    mode: 'groups',             // 'groups' | 'po' | 'islands'
    selectedGroups:  new Set(),
    selectedPOs:     new Set(),
    selectedIslands: new Set(),
    includeOrphans:  false,
    maxRelationDistance: maxAvailableDistance,
    focusSet: null,
  };

  // we show first group by default if available
  if (groups.length > 0) {
    state.selectedGroups.add(0);
  }

  let lastScene = { nodes: [], edges: [] };

  // chip containers will be assigned after createPillMenu
  let groupChipContainer   = null;
  let poChipContainer      = null;
  let islandChipContainer  = null;

  const selectAllTimers = [];

  function cancelSelectAllTimers() {
    while (selectAllTimers.length) {
      const id = selectAllTimers.pop();
      clearTimeout(id);
    }
  }

  // ---- SCENE BUILDERS ----

  function buildSceneForNodeSet(uuidSet, { groupColorByUuid = null } = {}) {
    const baseScene = buildSceneForNodeSetBase({
      uuidSet,
      baseNodes,
      allEdges: allRelationEdges,
      prevScene: lastScene,
    });

    for (const node of baseScene.nodes) {
      const base = baseNodes.get(node.id);
      const isPrimary = base.isPrimary;

      let highlightColor = null;

      if (groupColorByUuid && groupColorByUuid.has(node.id)) {
        highlightColor = groupColorByUuid.get(node.id);
      } else if (isPrimary) {
        // give primaries a subtle neutral highlight when not color-coded
        highlightColor = '#888888';
      }
      node.highlightColor = highlightColor;
    }

    // relation distance filter
    const filteredEdges = baseScene.edges.filter(e => {
      if (e.kind !== 'relation') return true;
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

    // neighbors = any node that shares a relation edge with a primary
    const uuidSet = new Set(primarySet);
    for (const e of allRelationEdges) {
      if (primarySet.has(e.source)) uuidSet.add(e.target);
      if (primarySet.has(e.target)) uuidSet.add(e.source);
    }

    return buildSceneForNodeSet(uuidSet, {
      groupColorByUuid,
    });
  }

  // PO VIEW: POs + primaries in groups they touch (+ optional orphans)
  function buildSceneForPOView() {
    if (!state.selectedPOs.size) return { nodes: [], edges: [] };

    const uuidSet = new Set();

    const poByUuid = new Map(poView.map(p => [p.po_uuid, p]));
    state.selectedPOs.forEach(poUuid => {
      uuidSet.add(poUuid);
      const entry = poByUuid.get(poUuid);
      if (!entry) return;

      (entry.group_indices || []).forEach(gIdx => {
        const g = groups[gIdx];
        if (!g) return;
        (g.entity_uuids || []).forEach(u => uuidSet.add(u));
      });
    });

    if (state.includeOrphans) {
      orphans.forEach(o => {
        (o.uuids || []).forEach(u => uuidSet.add(u));
      });
    }

    // color primaries by their duplicity group
    const groupColorByUuid = new Map();
    uuidSet.forEach(u => {
      if (!duplicatedPrimaries.has(u)) return;
      const gIdx = primaryGroupIndexByUuid.get(u);
      if (gIdx == null) return;
      const color = colorForGroupIndex(gIdx);
      if (!groupColorByUuid.has(u)) {
        groupColorByUuid.set(u, color);
      }
    });

    return buildSceneForNodeSet(uuidSet, {
      groupColorByUuid,
    });
  }

  // ISLANDS VIEW: all primaries from groups in selected islands
  function buildSceneForIslands() {
    if (!state.selectedIslands.size) return { nodes: [], edges: [] };

    const currentIslands = getIslandsForCurrentDistance();

    const primarySet = new Set();
    const groupColorByUuid = new Map();

    state.selectedIslands.forEach(idx => {
      const isl = currentIslands[idx];
      if (!isl) return;

      const groupIdxs = isl.groups || isl.group_indices || [];
      groupIdxs.forEach(gIdx => {
        const g = groups[gIdx];
        if (!g) return;
        const color = colorForGroupIndex(gIdx);
        (g.entity_uuids || []).forEach(u => {
          primarySet.add(u);
          if (!groupColorByUuid.has(u)) {
            groupColorByUuid.set(u, color);
          }
        });
      });
    });

    if (!primarySet.size) return { nodes: [], edges: [] };

    // neighbours = any node that shares a relation edge with a primary
    const uuidSet = new Set(primarySet);
    for (const e of allRelationEdges) {
      if (primarySet.has(e.source)) uuidSet.add(e.target);
      if (primarySet.has(e.target)) uuidSet.add(e.source);
    }

    if (state.includeOrphans) {
      orphans.forEach(o => {
        (o.uuids || []).forEach(u => uuidSet.add(u));
      });
    }

    return buildSceneForNodeSet(uuidSet, {
      groupColorByUuid,
    });
  }

  function updateScene() {
    if (!viewport || !physics) return;

    let scene;

    if (state.focusSet && state.focusSet.size) {
      scene = buildSceneForNodeSet(state.focusSet);
    } else if (state.mode === 'groups') {
      scene = buildSceneForGroups(state.selectedGroups);
    } else if (state.mode === 'po') {
      scene = buildSceneForPOView();
    } else if (state.mode === 'islands') {
      scene = buildSceneForIslands();
    } else {
      scene = { nodes: [], edges: [] };
    }

    viewport.setScene(scene);
    physics.setGraph(scene.nodes, scene.edges);
    lastScene = scene;
  }

  function limitSelectionTo(nodeId, maxDist) {
    const nb = collectNeighborsWithin(adjacency, nodeId, maxDist);
    state.focusSet = nb;
    updateScene();
  }

  function removeSelectionAround(nodeId, maxDist) {
    if (!state.focusSet) {
      const baseSet = new Set();
      (lastScene.nodes || []).forEach(n => baseSet.add(n.id));
      state.focusSet = baseSet;
    }

    const toRemove = collectNeighborsWithin(adjacency, nodeId, maxDist);
    toRemove.forEach(id => state.focusSet.delete(id));
    updateScene();
  }

  // we need graphPanel for the context menu callback
  ensureCssLoaded();
  container.innerHTML = '';
  const graphPanel = container;
  graphPanel.classList.add('graph-panel');

  function showDupContextMenu(node, event) {
    const items = [];

    items.push({
      label: 'Limit selection to this element',
      onClick: () => limitSelectionTo(node.id, 0),
    });

    [0, 1, 2, 3, Infinity].forEach(dist => {
      const label =
        dist === Infinity
          ? 'Limit selection: element and all related'
          : `Limit selection: element and related up to ${dist}`;
      items.push({
        label,
        onClick: () => limitSelectionTo(node.id, dist),
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
        onClick: () => removeSelectionAround(node.id, dist),
      });
    });

    showContextMenu(graphPanel, event, items);
  }

  // ---------- CHIP UI LOGIC (groups / PO / islands) ----------
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
        state.focusSet = null;
        // switch to groups mode
        if (state.mode !== 'groups') {
          state.mode = 'groups';
          state.selectedPOs.clear();
          state.selectedIslands.clear();
          renderPOChips();
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

  function renderPOChips() {
    if (!poChipContainer) return;
    poChipContainer.innerHTML = '';

    poView.forEach((entry) => {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'app-header-chip';

      const label = entry.identifier || entry.po_uuid;
      const gCount = entry.group_count ?? (entry.group_indices || []).length;
      chip.textContent = `${label} (${gCount} groups)`;

      if (state.selectedPOs.has(entry.po_uuid)) {
        chip.classList.add('app-header-chip-active');
      }

      chip.addEventListener('click', () => {
        state.focusSet = null;
        // switch to PO mode
        if (state.mode !== 'po') {
          state.mode = 'po';
          state.selectedGroups.clear();
          state.selectedIslands.clear();
          renderGroupChips();
          renderIslandChips();
        }

        if (state.selectedPOs.has(entry.po_uuid)) {
          state.selectedPOs.delete(entry.po_uuid);
        } else {
          state.selectedPOs.add(entry.po_uuid);
        }

        renderPOChips();
        updateScene();
      });

      poChipContainer.appendChild(chip);
    });
  }

  function renderIslandChips() {
    if (!islandChipContainer) return;
    islandChipContainer.innerHTML = '';

    const currentIslands = getIslandsForCurrentDistance();

    currentIslands.forEach((island, idx) => {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'app-header-chip';

      const groupIdxs = island.groups || island.group_indices || [];
      const sampleCodes = groupIdxs
        .map(gIdx => groups[gIdx])
        .filter(Boolean)
        .map(g => g.metais_code || '')
        .filter(Boolean)
        .slice(0, 3);

      const extra = groupIdxs.length - sampleCodes.length;

      let text = `Island ${idx + 1} (${island.count ?? '?'} nodes`;
      if (sampleCodes.length) {
        text += `; groups: ${sampleCodes.join(', ')}`;
        if (extra > 0) text += ` +${extra}`;
      }
      text += ')';

      chip.textContent = text;

      if (state.selectedIslands.has(idx)) {
        chip.classList.add('app-header-chip-active');
      }

      chip.addEventListener('click', () => {
        state.focusSet = null;
        // switch to islands mode
        state.mode = 'islands';
        state.selectedGroups.clear();
        state.selectedPOs.clear();
        renderGroupChips();
        renderPOChips();

        if (state.selectedIslands.has(idx)) {
          state.selectedIslands.delete(idx);
        } else {
          state.selectedIslands.add(idx);
        }

        renderIslandChips();
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

    const STEP_MS = 200;

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

  const ISLAND_CONTROLS = []; // nothing special (for now)

  const GLOBAL_CONTROLS = [
    {
      type: 'button',
      id: 'clearAll',
      label: 'Clear',
      onClick: () => {
        cancelSelectAllTimers();
        state.selectedGroups.clear();
        state.selectedPOs.clear();
        state.selectedIslands.clear();
        state.focusSet = null;

        renderGroupChips();
        renderPOChips();
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
          state.selectedPOs.clear();
          state.selectedIslands.clear();
          renderPOChips();
          renderIslandChips();
        }
        selectAllGradual();
      },
    },
    {
      type: 'chipGroup',
      id: 'relationDistance',
      label: 'Relations up to:',
      items: () => availableDistances,
      isActive: (v) => state.maxRelationDistance === v,
      onSelect: (v) => {
        // 1) remember which groups are represented by currently selected islands
        const oldIslands = getIslandsForCurrentDistance();
        const selectedGroups = new Set();
        state.selectedIslands.forEach(idx => {
          const isl = oldIslands[idx];
          if (!isl) return;
          const groupIdxs = isl.groups || isl.group_indices || [];
          groupIdxs.forEach(g => selectedGroups.add(g));
        });

        // 2) update distance (pure number; max = “all”)
        state.maxRelationDistance = v;

        // 3) if we are in islands mode, remap selection onto new islands;
        //    otherwise clear island selection.
        if (state.mode === 'islands') {
          const newIslands = getIslandsForCurrentDistance();
          state.selectedIslands.clear();

          newIslands.forEach((isl, idx) => {
            const groupIdxs = isl.groups || isl.group_indices || [];
            const intersects = groupIdxs.some(g => selectedGroups.has(g));
            if (intersects) {
              state.selectedIslands.add(idx);
            }
          });
        } else {
          state.selectedIslands.clear();
        }

        renderIslandChips();
        updateScene();
      },
    },
  ];

  // ---- DOM + UI ----

  // Main graph row + canvas
  const mainRow = document.createElement('div');
  mainRow.className = 'graph-panel-main';
  graphPanel.appendChild(mainRow);

  const root = document.createElement('div');
  root.className = 'graph-root';
  mainRow.appendChild(root);

  // --- Floating pill bar: Groups / PO view / Islands ---
  const { bar: pillBar } = createPillMenu(graphPanel, [
    {
      id: 'groups',
      label: 'Groups',
      build: ({ controlsEl, chipsEl }) => {
        renderControlsRow(GROUPS_CONTROLS, controlsEl);
        groupChipContainer = chipsEl;
      },
    },
    {
      id: 'po',
      label: 'PO view',
      build: ({ controlsEl, chipsEl }) => {
        renderControlsRow([], controlsEl); // no extra PO-specific controls (for now)
        poChipContainer = chipsEl;
        renderPOChips();
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

  const getEdgeStyle    = makeMetaisEdgeStyle();
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
        softness:    DUP_GLOW_BLUR_TILES,
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
      if (!node || !evt) return;
      showDupContextMenu(node, evt);
    },
  });

  const physics = new PhysicsSystem({
    timeScale: 1.0,
    maxDt: 1.0,
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
        const relInfo = data.relations?.share_same_metaid || {};
        const titleText =
          relInfo.name ||
          edge.relName ||
          'Share a common MetaIS code';

        const srcEnt = data.entities?.[edge.source] || {};
        const dstEnt = data.entities?.[edge.target] || {};
        const srcType = srcEnt.type || 'Any';
        const dstType = dstEnt.type || 'Any';

        const title = document.createElement('div');
        title.className = 'graph-edge-title';
        title.textContent = titleText;

        const types = document.createElement('div');
        types.className = 'graph-edge-types';
        types.textContent = `${srcType} ↔ ${dstType}`;

        el.appendChild(title);
        el.appendChild(types);
        return;
      }

      // normal relations
      const relInfo = data.relations?.[edge.relName] || {};
      const humanName =
        relInfo.name ||
        edge.relName ||
        '(neznámy vzťah)';

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
      title.textContent = humanName;

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
  renderPOChips();
  renderIslandChips();
  updateScene();

  // --- Animation loop: physics + redraw ---
  let lastTime = performance.now();

  function tick(now) {
    const dtRaw = (now - lastTime) / 1000;
    lastTime = now;

    physics.step(dtRaw);
    viewport.draw();

    requestAnimationFrame(tick);
  }

  requestAnimationFrame(tick);
}