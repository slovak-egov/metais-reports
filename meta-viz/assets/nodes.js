// frontend logic for nodes

// --- Configuration for thresholds ---
const REQUIRED_THRESHOLD = 100; // >= 100% => "required"
const COMMON_THRESHOLD = 10;   // 50–95% => "common"; else rare

// shortlisted entities
const CURATED_TYPES = [
  "KS",
  "AS",
  "InfraSluzba",
  "ISVS",
  "KRIS",
  "Program",
  "Projekt",
  "Formular"
];

// --- Global state ---
const state = {
  index: null,          // { snapshots: [{date, node_types: [...]}, ...] }
  statsCache: {},       // key: `${date}:${nodeType}` -> stats object
  metadataIndex: null,
  currentSnapshot: null,
  currentNodeType: null,
  currentView: "nodes",
  filterText: "",
  limit: 50,
  typeFilters: {
    application: true,
    codelist: false,
    system: false,
    curated: false
  }
};

function updateSpriteForCurrentNodeType() {
  const img = document.getElementById("node-sprite");
  if (!img) return;

  const nodeType = state.currentNodeType;
  if (!nodeType) {
    img.style.display = "none";
    return;
  }

  const metaIdx = state.metadataIndex && state.metadataIndex.types;
  const typeMeta = metaIdx && metaIdx[nodeType];

  // primary sprite (same name as node type)
  const primarySrc = `data/sprites/${encodeURIComponent(nodeType)}.svg`;

  // fallback by category
  let fallbackSrc = "data/sprites/default_generic.svg";
  if (typeMeta) {
    if (typeMeta.isApplication && !typeMeta.isCodelist) {
      fallbackSrc = "data/sprites/default_application.svg";
    } else if (typeMeta.isSystem) {
      fallbackSrc = "data/sprites/default_system.svg";
    } else if (typeMeta.isCodelist) {
      fallbackSrc = "data/sprites/default_codelist.svg";
    }
  }

  img.style.display = "";
  img.dataset.fallbackApplied = "0";

  img.onerror = () => {
    // avoid infinite loop if fallback also fails
    if (img.dataset.fallbackApplied === "1") return;
    img.dataset.fallbackApplied = "1";
    img.src = fallbackSrc;
  };

  img.src = primarySrc;
}

// --- Data loading helpers ---

async function loadMetadataIndex() {
  try {
    const res = await fetch("data/metadata/node_index.json", { cache: "no-cache" });
    if (!res.ok) {
      console.warn("No node_index.json found, continuing without metadata.");
      return null;
    }
    const data = await res.json();
    state.metadataIndex = data;
    return data;
  } catch (e) {
    console.warn("Failed to load node_index.json:", e);
    return null;
  }
}

async function loadIndex() {
  const res = await fetch("data/stats/index.json", { cache: "no-cache" });
  if (!res.ok) {
    throw new Error(`Failed to load data/stats/index.json: ${res.status}`);
  }
  const data = await res.json();
  state.index = data;
  return data;
}

async function loadAttributesSnapshot(snapshot, nodeType) {
  const key = `${snapshot}:${nodeType}`;
  if (state.statsCache[key]) {
    return state.statsCache[key];
  }

  const url = `data/stats/${encodeURIComponent(snapshot)}/attributes/${encodeURIComponent(nodeType)}.json`;
  const res = await fetch(url, { cache: "no-cache" });
  if (!res.ok) {
    throw new Error(`Failed to load ${url}: ${res.status}`);
  }
  const stats = await res.json();

  // Expect shape: { type, count, attributes: [{ name, count, pct }, ...] }
  // You can add light validation here if you want.

  state.statsCache[key] = stats;
  return stats;
}

// truncator helper
function truncateExpr(str, maxLen) {
  if (!str) return "";
  if (str.length <= maxLen) return str;
  return str.slice(0, maxLen - 1) + "…";  // or "..."
}


// nonzero percentage display helper
function formatPercentSmart(pct, normalDecimals = 2, maxDecimals = 8) {
  if (typeof pct !== "number" || !isFinite(pct)) return "0%";
  if (pct === 0) return "0%";

  const abs = Math.abs(pct);

  // "Normal" values: just use fixed decimals
  if (abs >= 0.01) {
    return pct.toFixed(normalDecimals) + "%";
  }

  // Very small but non-zero:
  // choose enough decimals so that we get ~2 significant digits,
  // but not more than maxDecimals.
  const desiredSig = 2;
  const log10 = Math.log10(abs);
  // abs in (0, 0.01) ⇒ -log10 > 2
  let decimals = Math.ceil(-log10) + (desiredSig - 1);
  decimals = Math.min(maxDecimals, Math.max(2, decimals));

  let str = pct.toFixed(decimals);

  // strip trailing zeros
  str = str
    .replace(/(\.\d*?[1-9])0+$/, "$1")  // 0.005900 → 0.0059
    .replace(/\.0+$/, "");              // 0.0100   → 0.01

  return str + "%";
}

// --- UI wiring ---

function initControls() {
  const snapshotSelect = document.getElementById("snapshot-select");
  const nodeTypeSelect = document.getElementById("nodetype-select");
  //const reloadBtn = document.getElementById("reload-btn");
  const viewAttrBtn = document.getElementById("view-nodes");
  const viewRelBtn = document.getElementById("view-relations");
  const viewHybBtn = document.getElementById("view-hybrid");
  const filterInput = document.getElementById("filter-input");
  const limitSelect = document.getElementById("limit-select");

  const cbApplication = document.getElementById("filter-application");
  const cbCodelist = document.getElementById("filter-codelist");
  const cbSystem = document.getElementById("filter-system");
  const cbCurated = document.getElementById("filter-curated");

  snapshotSelect.addEventListener("change", () => {
    state.currentSnapshot = snapshotSelect.value;
    updateNodeTypesForSnapshot();
    refreshView();
  });

  nodeTypeSelect.addEventListener("change", () => {
    state.currentNodeType = nodeTypeSelect.value;
    refreshView();
  });

  /*reloadBtn.addEventListener("click", async () => {
    try {
      state.statsCache = {}; // clear cache
      await loadIndex();
      populateSnapshotSelect();
      updateNodeTypesForSnapshot();
      refreshView();
    } catch (e) {
      console.error(e);
      alert("Failed to reload index.json – see console for details.");
    }
  });*/

  viewAttrBtn.addEventListener("click", () => {
    state.currentView = "nodes";
    setActiveViewButton("nodes");
    refreshView();
  });

  viewRelBtn.addEventListener("click", () => {
    // stub for future relations view
    state.currentView = "relations";
    setActiveViewButton("relations");
    refreshView();
  });

  viewHybBtn.addEventListener("click", () => {
    // stub for future hybrid view
    state.currentView = "hybrid";
    setActiveViewButton("hybrid");
    refreshView();
  });

  filterInput.addEventListener("input", () => {
    state.filterText = filterInput.value.trim().toLowerCase();
    refreshView();
  });

  limitSelect.addEventListener("change", () => {
    const v = parseInt(limitSelect.value, 10);
    state.limit = isNaN(v) ? 50 : v;
    refreshView();
  });

  function setActiveViewButton(view) {
    viewAttrBtn.classList.toggle("view-btn-active", view === "nodes");
    viewRelBtn.classList.toggle("view-btn-active", view === "relations");
    // viewHybridBtn?.classList.toggle("view-btn-active", view === "hybrid");
  }

  function updateFiltersAndRefresh() {
    updateNodeTypesForSnapshot();
    refreshView();
  }

  cbApplication.addEventListener("change", () => {
    state.typeFilters.application = cbApplication.checked;
    updateFiltersAndRefresh();
  });

  cbCodelist.addEventListener("change", () => {
    state.typeFilters.codelist = cbCodelist.checked;
    updateFiltersAndRefresh();
  });

  cbSystem.addEventListener("change", () => {
    state.typeFilters.system = cbSystem.checked;
    updateFiltersAndRefresh();
  });

  cbCurated.addEventListener("change", () => {
    state.typeFilters.curated = cbCurated.checked;

    // curated disables other filters (as you requested)
    cbApplication.disabled = cbCurated.checked;
    cbCodelist.disabled = cbCurated.checked;
    cbSystem.disabled = cbCurated.checked;

    updateFiltersAndRefresh();
  });
}

function populateSnapshotSelect() {
  const snapshotSelect = document.getElementById("snapshot-select");
  snapshotSelect.innerHTML = "";

  const idx = state.index;
  if (!idx || !Array.isArray(idx.snapshots)) return;

  for (const snap of idx.snapshots) {
    const opt = document.createElement("option");
    opt.value = snap.date;
    opt.textContent = snap.date;
    opt.dataset.nodeTypes = JSON.stringify(snap.node_types || []);
    snapshotSelect.appendChild(opt);
  }

  // Default to the latest snapshot (lexicographically last date)
  if (!state.currentSnapshot && idx.snapshots.length > 0) {
    state.currentSnapshot =
      idx.snapshots[idx.snapshots.length - 1].date;
  }

  snapshotSelect.value = state.currentSnapshot || "";
}

function updateNodeTypesForSnapshot() {
  const snapshotSelect = document.getElementById("snapshot-select");
  const nodeTypeSelect = document.getElementById("nodetype-select");

  const selectedOption = snapshotSelect.options[snapshotSelect.selectedIndex];
  if (!selectedOption) {
    nodeTypeSelect.innerHTML = "";
    return;
  }

  const allNodeTypes = JSON.parse(selectedOption.dataset.nodeTypes || "[]");
  const nodeTypeMeta = state.metadataIndex && state.metadataIndex.types;
  const filters = state.typeFilters;

  let filtered = allNodeTypes.slice();

  // If curated is on, ignore the other filters and only show curated types
  if (filters.curated) {
    filtered = filtered.filter((nt) => CURATED_TYPES.includes(nt));
  } else if (nodeTypeMeta) {
    const wantApp = filters.application;
    const wantCode = filters.codelist;
    const wantSys = filters.system;

    // if at least one of the three is on, filter by them
    if (wantApp || wantCode || wantSys) {
      filtered = filtered.filter((nt) => {
        const m = nodeTypeMeta[nt];
        if (!m) return false;

        if (wantCode && m.isCodelist) return true;
        if (wantApp && m.isApplication && !m.isCodelist) return true;
        if (wantSys && m.isSystem) return true;

        return false;
      });
    }
  }

  nodeTypeSelect.innerHTML = "";

  for (const nt of filtered) {
    const opt = document.createElement("option");
    opt.value = nt;

    // metadata-based label
    const metaIdx = state.metadataIndex && state.metadataIndex.types;
    const typeMeta = metaIdx && metaIdx[nt];
    if (typeMeta && typeMeta.name) {
      opt.textContent = `${typeMeta.name} (${nt})`;
    } else {
      opt.textContent = nt;
    }

    nodeTypeSelect.appendChild(opt);
  }

  // pick current node type if still present, otherwise fall back to first filtered
  if (!filtered.includes(state.currentNodeType)) {
    state.currentNodeType = filtered.length > 0 ? filtered[0] : null;
  }

  nodeTypeSelect.value = state.currentNodeType || "";
}

// --- Rendering ---

async function refreshView() {
  const snapshot = state.currentSnapshot;
  const nodeType = state.currentNodeType;
  const breadcrumbEl = document.getElementById("breadcrumb");
  const summaryEl = document.getElementById("summary");

  if (!snapshot || !nodeType) {
    breadcrumbEl.textContent = "Select snapshot and node type";
    summaryEl.textContent = "";
    document.getElementById("chart").innerHTML = "";
    document.getElementById("attr-tbody").innerHTML = "";
    updateSpriteForCurrentNodeType();
    return;
  }

  breadcrumbEl.textContent = `Snapshot ${snapshot} · Node ${nodeType}`;
  updateSpriteForCurrentNodeType();

  try {
    if (state.currentView === "nodes") {
      summaryEl.textContent = "Loading node attribute stats…";
      const stats = await loadAttributesSnapshot(snapshot, nodeType);
      renderAttributes(stats);
    } else if (state.currentView === "relations") {
      // Temporary placeholder: just show an empty panel
      summaryEl.textContent = `Relations view is under construction for ${nodeType}.`;
      clearMainContent();
    } else if (state.currentView === "hybrid") {
      // Hybrid not implemented yet; also show placeholder
      summaryEl.textContent = `Hybrid view is under construction for ${nodeType}.`;
      clearMainContent();
    }
  } catch (e) {
    console.error(e);
    summaryEl.textContent = "Failed to load data for this view.";
    clearMainContent();
  }

  function clearMainContent() {
    document.getElementById("chart").innerHTML = "";
    document.getElementById("attr-tbody").innerHTML = "";
  }
}

function renderAttributes(stats) {
  const summaryEl = document.getElementById("summary");
  const chartEl = document.getElementById("chart");
  const tbody = document.getElementById("attr-tbody");

  const total = stats.count || 0;
  const nodeType = stats.type || "?";

  summaryEl.textContent =
    total > 0
      ? `${total} objects in ${nodeType}. Showing attribute coverage.`
      : `No objects for ${nodeType}.`;

  let rows = Array.isArray(stats.attributes) ? stats.attributes.slice() : [];
  const metaIdx = state.metadataIndex && state.metadataIndex.types;
  const typeMeta = metaIdx && metaIdx[nodeType];
  const attrMetaMap = typeMeta && typeMeta.attributes ? typeMeta.attributes : {};

  // Filter
  if (state.filterText) {
    rows = rows.filter((r) =>
      String(r.name || "").toLowerCase().includes(state.filterText)
    );
  }

  // Limit
  if (state.limit > 0) {
    rows = rows.slice(0, state.limit);
  }

  // Chart
  chartEl.innerHTML = "";
  for (const r of rows) {
    const rowDiv = document.createElement("div");
    rowDiv.className = "chart-row";

    const nameSpan = document.createElement("div");
    nameSpan.className = "chart-attr-name";
    nameSpan.textContent = r.name;

    const barOuter = document.createElement("div");
    barOuter.className = "chart-bar-outer";

    const barInner = document.createElement("div");
    barInner.className = "chart-bar-inner";

    const pct = typeof r.pct === "number" ? r.pct : 0;
    const clampedPct = Math.max(0, Math.min(100, pct));

    const tinyThreshold = 3;     // below 1% -> draw pill
    const minPillPx = 12;         // width of pill for tiny non-zero values

    if (clampedPct <= 0) {
      // zero coverage -> no bar
      barInner.style.width = "0";
    } else if (clampedPct < tinyThreshold) {
      // tiny but non-zero -> render as small pill
      barInner.style.width = `${minPillPx}px`;
    } else {
      // normal case -> percentage width
      barInner.style.width = `${clampedPct.toFixed(2)}%`;
    }

    if (pct >= REQUIRED_THRESHOLD) {
      // default gradient (strong)
    } else if (pct >= COMMON_THRESHOLD) {
      barInner.classList.add("common");
    } else {
      barInner.classList.add("rare");
    }

    barOuter.appendChild(barInner);

    const uniqueCount = typeof r.unique_count === "number" ? r.unique_count : null;
    const uniquePctFilled = typeof r.unique_pct === "number" ? r.unique_pct : null;

    if (uniqueCount != null && uniquePctFilled != null && r.count > 0) {
      const uniqueWidth = pct * (uniquePctFilled / 100); // in %
      const offsetPx = 3;   // matches CSS left: 2px
      const minPillWidthPx = 6;  // small "dot" width
    
      const uniqueInner = document.createElement("div");
      uniqueInner.className = "chart-bar-inner-unique";
      uniqueInner.title =
        `${uniqueCount} unique values (${uniquePctFilled.toFixed(1)}% of filled)`;
    
      // If the slice would be really tiny, just render a small pill
      if (uniqueWidth < 5) { // threshold in %
        uniqueInner.style.left = `${offsetPx}px`;
        uniqueInner.style.width = `${minPillWidthPx}px`;
      } else {
        // normal case: subtract the left offset so we don't overshoot
        // width ≈ uniqueWidth% of the bar minus the offset
        uniqueInner.style.left = `${offsetPx}px`;
        uniqueInner.style.width = `calc(${uniqueWidth.toFixed(2)}% - 2 * ${offsetPx}px)`;
      }
    
      barOuter.appendChild(uniqueInner);
    }

    const pctSpan = document.createElement("div");
    pctSpan.className = "chart-pct";
    pctSpan.textContent = formatPercentSmart(pct, 3, 8);

    rowDiv.appendChild(nameSpan);
    rowDiv.appendChild(barOuter);
    rowDiv.appendChild(pctSpan);
    chartEl.appendChild(rowDiv);
  }

  // Table
  tbody.innerHTML = "";
  for (const r of rows) {
    const tr = document.createElement("tr");
    const tdName = document.createElement("td");
    const tdCount = document.createElement("td");
    const tdUnique = document.createElement("td");
    const tdPct = document.createElement("td");

    const techName = r.name;
    const meta = attrMetaMap[techName];

    let displayName = techName;
    let subtitleFull = "";
    let isCritical = false;

    if (meta) {
    if (meta.name) {
        displayName = `${meta.name} (${techName})`;
    }
    if (meta.mandatory === "critical") {
        isCritical = true;
    }
    if (meta.description) {
        subtitleFull = meta.description;
    }
    }

    tdName.innerHTML = "";

    // main label
    const mainSpan = document.createElement("div");
    mainSpan.textContent = displayName + (isCritical ? " *" : "");
    tdName.appendChild(mainSpan);

    // --- description line ---
    let subtitleToShow = subtitleFull && subtitleFull.trim().length > 0
    ? subtitleFull
    : "(missing attribute description)";

    const nameLen = displayName.length;
    const maxDescLen = 100;

    const subSpan = document.createElement("div");
    subSpan.className = "attr-subtitle";
    subSpan.textContent = truncateExpr(subtitleToShow, maxDescLen);
    subSpan.title = subtitleToShow;

    tdName.appendChild(subSpan);

    tdCount.textContent = r.count;
    const pct = typeof r.pct === "number" ? r.pct : 0;
    tdPct.textContent = formatPercentSmart(pct, 3, 8);

    const uniqueCountCell =
      typeof r.unique_count === "number" ? r.unique_count : null;
    tdUnique.textContent = uniqueCountCell != null ? uniqueCountCell : "–";

    tdName.className = "col-name";
    tdCount.className = "col-count";
    tdUnique.className = "col-unique";
    tdPct.className = "col-pct";

    tr.appendChild(tdName);
    tr.appendChild(tdCount);
    tr.appendChild(tdUnique);
    tr.appendChild(tdPct);

    tbody.appendChild(tr);
  }
}

// --- Bootstrapping ---

document.addEventListener("DOMContentLoaded", async () => {
  initControls();

  try {
    await Promise.all([
      loadIndex(),
      loadMetadataIndex()
    ]);

    populateSnapshotSelect();
    updateNodeTypesForSnapshot();
    refreshView();
  } catch (e) {
    console.error(e);
    alert("Failed to initialize meta-viz – make sure data/stats/index.json exists.");
  }
});