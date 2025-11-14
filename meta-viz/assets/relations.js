// frontend logic for relations

// shortlisted relations
const CURATED_TYPES = [
  "PO_je_gestor_KS"
];

const relState = {
  relationIndex: null,   // data/metadata/relation_index.json
  nodeIndex: null,       // data/metadata/node_index.json (for type names)
  snapshotIndex: null,   // data/stats/index.json (same as nodes)
  currentSnapshot: null,
  currentRelation: null,
  statsCache: {},        // key: snapshot:relation -> stats object
  typeFilters: {
    application: true,
    system: false,
    curated: false
  }
};

function parseRGBA(str) {
  const m = str.match(/rgba?\(([^)]+)\)/);
  if (!m) return [0, 0, 0, 1];
  const parts = m[1].split(",").map(x => parseFloat(x.trim()));
  while (parts.length < 4) parts.push(1); // default alpha = 1
  return parts.slice(0, 4);
}

// pie charts
const viewBoxSize = 80;
const radius = 24;
const circumference = 2 * Math.PI * radius;
const strokeWidth = 20;
const cx = 40;
const cy = 40;

const colDefault = "rgba(55, 65, 81, 0.8)";

function renderCoveragePie(total, connected, parallels, edges_total) {
  if (!total || total <= 0 || connected == null || edges_total == 0) {
    // Just an empty grey ring if we have no data
    return `
      <div class="relation-coverage-chart" title="No coverage data">
        <svg viewBox="0 0 ${viewBoxSize} ${viewBoxSize}">
          <circle cx="${cx}" cy="${cy}" r="${radius}" fill="none"
                  stroke="${colDefault}" stroke-width="${strokeWidth}" />
        </svg>
      </div>
    `;
  }

  const frac = Math.max(0, Math.min(1, connected / total));
  const pctLabel = (frac * 100).toFixed(1) + "%";
  const dash = frac * circumference;
  
  const fracP = Math.max(0, Math.min(1, parallels / edges_total));
  const dashP = fracP * circumference;

  return `
    <div class="relation-coverage-chart"
         title="Connected: ${connected} / ${total} (${pctLabel})">
      <svg viewBox="0 0 ${viewBoxSize} ${viewBoxSize}">
        <!-- background ring -->
        <circle cx="${cx}" cy="${cy}" r="${radius}" fill="none"
                stroke="${colDefault}" stroke-width="${strokeWidth}" />
        <!-- coverage arc -->
        <circle cx="${cx}" cy="${cy}" r="${radius}" fill="none"
                stroke="#2fbff8ff" stroke-width="${strokeWidth}"
                stroke-dasharray="${dash} ${circumference - dash}"
                stroke-dashoffset="0"
                transform="rotate(-90 ${cx} ${cy})" />
        <!-- parallels -->
        <circle cx="${cx}" cy="${cy}" r="${radius}" fill="none"
                stroke="#e71616ff" stroke-width="${strokeWidth}"
                stroke-dasharray="${dashP} ${circumference - dashP}"
                stroke-dashoffset="0"
                transform="rotate(-90 ${cx} ${cy})" />
      </svg>
    </div>
  `;
}

function renderIslandPie(fractions) {
  // clean + sort: descending by size
  let parts = (fractions || [])
    .map(Number)
    .filter((f) => isFinite(f) && f > 0);

  if (!parts.length) {
    // no islands -> just a gray ring
    return `
      <div class="relation-islands-chart" title="No multi-node islands">
        <svg viewBox="0 0 ${viewBoxSize} ${viewBoxSize}">
          <circle cx="${cx}" cy="${cy}" r="${radius}" fill="none"
                  stroke="${colDefault}" stroke-width="${strokeWidth}" />
        </svg>
      </div>
    `;
  }

  // normalize so they fill the whole circle (sum = 1)
  const sum = parts.reduce((s, f) => s + f, 0);
  if (sum <= 0) {
    return `
      <div class="relation-islands-chart" title="No multi-node islands">
        <svg viewBox="0 0 ${viewBoxSize} ${viewBoxSize}">
          <circle cx="${cx}" cy="${cy}" r="${radius}" fill="none"
                  stroke="${colDefault}" stroke-width="${strokeWidth}" />
        </svg>
      </div>
    `;
  }
  parts = parts.map((f) => f / sum); // now sum(parts) === 1

  // largest first (just for color mapping)
  parts.sort((a, b) => b - a);

  let svgArcs = "";
  let offsetFrac = 0;
  const n = parts.length;
  const hueStart = 0;   // red
  const hueEnd = 50;    // yellow-ish
  //rgba(255, 0, 170, 1)
  const colors = [
    [235,  10,  10, 1],
    [220, 120,   0, 1],
    [ 15, 170,   0, 1],
    [  0, 150, 220, 1],
    [160,  60, 255, 1],
    [255,   0, 170, 1]
  ];
  const colDefaultArr = parseRGBA(colDefault);

  const maxFrac = Math.max(...parts);
  const renderThreshold = 0.05;

  parts.forEach((frac, idx) => {
    const arcLen = frac * circumference;
  
    // cycle through pre-determined colors, darken as fractions drop to 0
    const vivid = colors[idx % colors.length];
    const tBase = maxFrac > 0 ? frac / maxFrac : 0;
    const t = frac > renderThreshold ? 1 : (frac / renderThreshold) * (frac / renderThreshold);

    const r = Math.round(colDefaultArr[0] + t * (vivid[0] - colDefaultArr[0]));
    const g = Math.round(colDefaultArr[1] + t * (vivid[1] - colDefaultArr[1]));
    const b = Math.round(colDefaultArr[2] + t * (vivid[2] - colDefaultArr[2]));
    const a = 1;

    const color = `rgba(${r}, ${g}, ${b}, ${a})`;
  
    svgArcs += `
      <circle cx="${cx}" cy="${cy}" r="${radius}" fill="none"
              stroke="${color}"
              stroke-width="${strokeWidth}"
              stroke-dasharray="${arcLen.toFixed(3)} ${(circumference - arcLen).toFixed(3)}"
              stroke-dashoffset="${(-offsetFrac * circumference).toFixed(3)}"
              transform="rotate(-90 ${cx} ${cy})" />
    `;
  
    offsetFrac += frac;
  });

  return `
    <div class="relation-islands-chart"
         title="Island sizes normalized over connected entities">
      <svg viewBox="0 0 ${viewBoxSize} ${viewBoxSize}">
        ${svgArcs}
      </svg>
    </div>
  `;
}

// safely inject text
function escapeHtml(str) {
  if (str == null) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

async function loadJSON(url) {
  const res = await fetch(url, { cache: "no-cache" });
  if (!res.ok) {
    throw new Error(`${url} -> HTTP ${res.status}`);
  }
  return res.json();
}

async function initRelations() {
  const snapshotSelect = document.getElementById("snapshot-select");
  const relationSelect = document.getElementById("relation-select");
  const reloadBtn = document.getElementById("reload-btn");
  const cbApplication = document.getElementById("filter-application");
  const cbSystem = document.getElementById("filter-system");
  const cbCurated = document.getElementById("filter-curated");

  // Load indices
  const [relIdx, nodeIdx, snapIdx] = await Promise.all([
    loadJSON("data/metadata/relation_index.json"),
    loadJSON("data/metadata/node_index.json"),
    loadJSON("data/stats/index.json"),
  ]);

  relState.relationIndex = relIdx.relations || {};
  relState.nodeIndex = nodeIdx.types || {};
  relState.snapshotIndex = snapIdx;

  populateSnapshots();
  populateRelationsForSnapshot();
  refreshRelationView();

  snapshotSelect.addEventListener("change", () => {
    relState.currentSnapshot = snapshotSelect.value;
    populateRelationsForSnapshot();
    refreshRelationView();
  });

  relationSelect.addEventListener("change", () => {
    relState.currentRelation = relationSelect.value;
    refreshRelationView();
  });

  reloadBtn.addEventListener("click", async () => {
    // simplest: full page reload
    window.location.reload();
  });

  // --- checkbox wiring lives INSIDE initRelations ---

  function updateFiltersAndRefresh() {
    populateRelationsForSnapshot();
    refreshRelationView();
  }

  cbApplication.addEventListener("change", () => {
    relState.typeFilters.application = cbApplication.checked;
    updateFiltersAndRefresh();
  });

  cbSystem.addEventListener("change", () => {
    relState.typeFilters.system = cbSystem.checked;
    updateFiltersAndRefresh();
  });

  cbCurated.addEventListener("change", () => {
    relState.typeFilters.curated = cbCurated.checked;

    // when curated is on, freeze the other checkboxes
    cbApplication.disabled = cbCurated.checked;
    cbSystem.disabled = cbCurated.checked;

    updateFiltersAndRefresh();
  });

  // initial state
  cbApplication.disabled = false;
  cbSystem.disabled = false;
}

function populateSnapshots() {
  const snapshotSelect = document.getElementById("snapshot-select");
  snapshotSelect.innerHTML = "";
  const snaps = relState.snapshotIndex.snapshots || [];
  for (const snap of snaps) {
    const opt = document.createElement("option");
    opt.value = snap.date;
    opt.textContent = snap.date;
    snapshotSelect.appendChild(opt);
  }
  if (!relState.currentSnapshot && snaps.length > 0) {
    relState.currentSnapshot = snaps[snaps.length - 1].date;
  }
  snapshotSelect.value = relState.currentSnapshot || "";
}

// For now: just list all relations that have a stats file for this snapshot later.
// Placeholder: show all known relations.
function populateRelationsForSnapshot() {
  const relationSelect = document.getElementById("relation-select");
  relationSelect.innerHTML = "";

  const snap = relState.currentSnapshot;
  const snapshots = relState.snapshotIndex?.snapshots || [];
  const relIndex = relState.relationIndex || {};
  const filters = relState.typeFilters;

  if (!snap) return;

  const snapEntry = snapshots.find((s) => s.date === snap);
  const allRelNames = snapEntry?.relations ? snapEntry.relations.slice() : [];

  let filtered = allRelNames.slice();

  if (filters.curated) {
    // curated: only relations from CURATED_TYPES intersection
    filtered = filtered.filter((name) => CURATED_TYPES.includes(name));
  } else {
    const wantApp = filters.application;
    const wantSys = filters.system;

    // if at least one filter is ON, restrict; if both OFF, show all
    if (wantApp || wantSys) {
      filtered = filtered.filter((name) => {
        const meta = relIndex[name];
        if (!meta) return false;
        const t = meta.type; // "application", "system", ...

        if (wantApp && t === "application") return true;
        if (wantSys && t === "system") return true;

        return false;
      });
    }
  }

  // populate select
  for (const name of filtered.sort()) {
    const meta = relIndex[name] || {};
    const label = meta.name ? `${meta.name} (${name})` : name;

    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = label;
    relationSelect.appendChild(opt);
  }

  // keep current selection if possible, otherwise pick first
  if (!filtered.includes(relState.currentRelation)) {
    relState.currentRelation = filtered.length > 0 ? filtered[0] : null;
  }

  relationSelect.value = relState.currentRelation || "";
}

async function loadRelationStats(snapshot, relation) {
  const key = `${snapshot}:${relation}`;
  if (Object.prototype.hasOwnProperty.call(relState.statsCache, key)) {
    return relState.statsCache[key];
  }

  const url = `data/stats/${encodeURIComponent(snapshot)}/relation_attributes/${encodeURIComponent(relation)}.json`;

  try {
    const stats = await loadJSON(url);
    relState.statsCache[key] = stats;
    return stats;
  } catch (e) {
    console.warn("Failed to load relation stats", { url, error: e });
    relState.statsCache[key] = null;
    return null;
  }
}

async function refreshRelationView() {
  const breadcrumb = document.getElementById("breadcrumb");
  const summary = document.getElementById("summary");
  const headerEl = document.getElementById("relation-header");
  const srcEl = document.getElementById("relation-source");
  const tgtEl = document.getElementById("relation-target");

  const snap = relState.currentSnapshot;
  const relName = relState.currentRelation;

  if (!snap || !relName) {
    breadcrumb.textContent = "Select snapshot and relation";
    summary.textContent = "";
    headerEl.innerHTML = "";
    srcEl.innerHTML = "";
    tgtEl.innerHTML = "";
    return;
  }

  breadcrumb.textContent = `Snapshot ${snap} · Relation ${relName}`;

  const meta = relState.relationIndex[relName];
  if (!meta) {
    summary.textContent = "No metadata for this relation.";
    headerEl.innerHTML = "";
    srcEl.innerHTML = "";
    tgtEl.innerHTML = "";
    return;
  }

  summary.textContent = "Loading relation stats…";
  const stats = await loadRelationStats(snap, relName);

  // --- header (middle arrow + description) ---
  const sourceLabel = meta.source?.name && meta.source?.technicalName
    ? `${meta.source.name} (${meta.source.technicalName})`
    : meta.source?.technicalName || "?";

  const targetLabel = meta.target?.name && meta.target?.technicalName
    ? `${meta.target.name} (${meta.target.technicalName})`
    : meta.target?.technicalName || "?";

  const shortDesc = (meta.description || "").trim();
  const descShort = shortDesc.length > 80
    ? shortDesc.slice(0, 77) + "…"
    : shortDesc;

  headerEl.innerHTML = `
    <div class="relation-header-main">
      <div class="relation-header-row">
        <div class="relation-source-label">${sourceLabel}</div>
        <div class="relation-arrow">→</div>
        <div class="relation-target-label">${targetLabel}</div>
      </div>
      <div class="relation-name">${meta.name || relName}</div>
      <div class="relation-desc" title="${shortDesc}">
        ${descShort || "(missing description)"}
      </div>
    </div>
  `;

  if (!stats) {
    summary.textContent = "No stats available for this relation in this snapshot.";
    srcEl.innerHTML = "";
    tgtEl.innerHTML = "";
    return;
  }

  const s = stats.stats || {};
  const edges = s.edges_total ?? 0;
  const uniquePairs = s.unique_pairs ?? 0;
  const parallels = s.parallel_edges ?? 0;
  const card = s.cardinality || "?";

  // islands -> fractions for the islands pie
  const srcIslandFractions = (s.islands?.source?.multi_islands || [])
    .map(i => typeof i.fraction === "number" ? i.fraction : 0)
    .filter(f => f > 0);
  
  const tgtIslandFractions = (s.islands?.target?.multi_islands || [])
    .map(i => typeof i.fraction === "number" ? i.fraction : 0)
    .filter(f => f > 0);

  const srcIslandsPie = renderIslandPie(srcIslandFractions);
  const tgtIslandsPie = renderIslandPie(tgtIslandFractions);

  const parallelText =
    parallels === 1
      ? "1 parallel edge"
      : `${parallels} parallel edges`;
  
  const parallelColor =
    parallels > 0 ? " style='color:#ef4444; font-weight:600;'" : "";

  summary.innerHTML =
    `<span class="summary-pill">${edges} edges</span> ` +
    `<span class="summary-pill">${uniquePairs} unique pairs</span> ` +
    `<span class="summary-pill"${parallelColor}>${parallelText}</span> ` +
    `<span class="summary-pill">cardinality: ${escapeHtml(card)}</span>`;

  // --- left side (source) ---
  const degSrc = s.degree_source || {};
  const degTgt = s.degree_target || {};
  const topSrc = Array.isArray(s.top_source) ? s.top_source : [];
  const topTgt = Array.isArray(s.top_target) ? s.top_target : [];
  
  const srcCoverage =
    s.source_total
      ? ((s.source_connected / s.source_total) * 100).toFixed(1) + "%"
      : "n/a";
  
  const srcPie = renderCoveragePie(s.source_total, s.source_connected, parallels, edges);
  
  srcEl.innerHTML = `
    <div class="relation-card">
      <div class="relation-coverage-charts">
        ${srcPie}
        ${srcIslandsPie}
      </div>
  
      <div class="relation-coverage-stats">
        <h3>Source: ${escapeHtml(sourceLabel)}</h3>
        <p class="relation-stat-line">
          Total nodes of this type: <strong>${s.source_total ?? "?"}</strong>
        </p>
        <p class="relation-stat-line">
          Connected nodes: <strong>${s.source_connected ?? 0}</strong>
        </p>
        <p class="relation-stat-line">
          Coverage: <strong>${srcCoverage}</strong>
        </p>
      </div>
  
      <div class="relation-subtitle">Degree distribution</div>
      <div class="relation-degree-grid">
        <div>min: <strong>${degSrc.min ?? "?"}</strong></div>
        <div>avg: <strong>${degSrc.avg?.toFixed?.(3) ?? degSrc.avg ?? "?"}</strong></div>
        <div>median: <strong>${degSrc.median ?? "?"}</strong></div>
        <div>p90: <strong>${degSrc.p90 ?? "?"}</strong></div>
        <div>p99: <strong>${degSrc.p99 ?? "?"}</strong></div>
        <div>max: <strong>${degSrc.max ?? "?"}</strong></div>
      </div>
  
      <div class="relation-subtitle">Top source nodes by degree</div>
      <div class="relation-top-list">
        ${topSrc.slice(0, 10).map(item => `
          <div class="relation-top-item">
            <div class="relation-top-main">
              <div class="relation-top-name">${escapeHtml(item.name || "(unnamed)")}</div>
              <div class="relation-top-metais">${escapeHtml(item.code || "")}</div>
            </div>
            <div class="relation-top-degree">${item.degree ?? "?"}</div>
          </div>
        `).join("") || "<div class='relation-empty'>No degree data available.</div>"}
      </div>
    </div>
  `;

  // --- right side (target) ---
  const tgtCoverage =
    s.target_total
      ? ((s.target_connected / s.target_total) * 100).toFixed(1) + "%"
      : "n/a";
  
  const tgtPie = renderCoveragePie(s.target_total, s.target_connected, parallels, edges);

  tgtEl.innerHTML = `
    <div class="relation-card">
      <div class="relation-coverage-charts">
        ${tgtPie}
        ${tgtIslandsPie}
      </div>
  
      <div class="relation-coverage-stats">
        <h3>Target: ${escapeHtml(targetLabel)}</h3>
        <p class="relation-stat-line">
          Total nodes of this type: <strong>${s.target_total ?? "?"}</strong>
        </p>
        <p class="relation-stat-line">
          Connected nodes: <strong>${s.target_connected ?? 0}</strong>
        </p>
        <p class="relation-stat-line">
          Coverage: <strong>${tgtCoverage}</strong>
        </p>
      </div>
  
      <div class="relation-subtitle">Degree distribution</div>
      <div class="relation-degree-grid">
        <div>min: <strong>${degTgt.min ?? "?"}</strong></div>
        <div>avg: <strong>${degTgt.avg?.toFixed?.(3) ?? degTgt.avg ?? "?"}</strong></div>
        <div>median: <strong>${degTgt.median ?? "?"}</strong></div>
        <div>p90: <strong>${degTgt.p90 ?? "?"}</strong></div>
        <div>p99: <strong>${degTgt.p99 ?? "?"}</strong></div>
        <div>max: <strong>${degTgt.max ?? "?"}</strong></div>
      </div>
  
      <div class="relation-subtitle">Top target nodes by degree</div>
      <div class="relation-top-list">
        ${topTgt.slice(0, 10).map(item => `
          <div class="relation-top-item">
            <div class="relation-top-main">
              <div class="relation-top-name">${escapeHtml(item.name || "(unnamed)")}</div>
              <div class="relation-top-metais">${escapeHtml(item.code || "")}</div>
            </div>
            <div class="relation-top-degree">${item.degree ?? "?"}</div>
          </div>
        `).join("") || "<div class='relation-empty'>No degree data available.</div>"}
      </div>
    </div>
  `;
}

document.addEventListener("DOMContentLoaded", () => {
  initRelations().catch((e) => {
    console.error(e);
    alert("Failed to initialize relations view.");
  });
});