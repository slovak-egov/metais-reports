const INDEX_URL = 'data/index.json';

let snapshots = [];   // loaded from index.json
let currentSort = 'newest';
let currentDate = null;

const snapshotSelect = document.getElementById('snapshot-select');
const categoryList   = document.getElementById('category-list');
const tileGrid       = document.getElementById('tile-grid');
const sortButtons    = document.querySelectorAll('.sort-btn');

async function fetchIndex() {
  const res = await fetch(INDEX_URL);
  if (!res.ok) {
    throw new Error(`Failed to load ${INDEX_URL}: ${res.status}`);
  }
  const data = await res.json();
  snapshots = data.snapshots || [];
}

// sort snapshots according to currentSort
function getSortedSnapshots() {
  const sorted = [...snapshots];
  sorted.sort((a, b) => {
    if (currentSort === 'newest') {
      return b.date.localeCompare(a.date);
    } else {
      return a.date.localeCompare(b.date);
    }
  });
  return sorted;
}

function populateSnapshotSelect() {
  snapshotSelect.innerHTML = '';
  const sorted = getSortedSnapshots();

  for (const snap of sorted) {
    const opt = document.createElement('option');
    opt.value = snap.date;
    opt.textContent = snap.date;
    snapshotSelect.appendChild(opt);
  }

  if (!currentDate && sorted.length > 0) {
    currentDate = sorted[0].date; // default to newest
  }
  if (currentDate) {
    snapshotSelect.value = currentDate;
  }
}

function attachSortButtons() {
  sortButtons.forEach(btn => {
    btn.addEventListener('click', () => {
      const sort = btn.dataset.sort;
      if (sort === currentSort) return;

      currentSort = sort;

      sortButtons.forEach(b => b.classList.toggle(
        'sort-btn-active',
        b.dataset.sort === currentSort
      ));

      populateSnapshotSelect();
      rebuildCategories();
    });
  });
}

function snapshotChanged() {
  currentDate = snapshotSelect.value;
  rebuildCategories();
}

// Find snapshot object by date
function getCurrentSnapshot() {
  return snapshots.find(s => s.date === currentDate) || null;
}

// Build category accordion for the currently selected date
function rebuildCategories() {
  categoryList.innerHTML = '';

  const snap = getCurrentSnapshot();
  if (!snap) {
    categoryList.textContent = 'No data for this date.';
    return;
  }

  const categories = snap.categories || {};

  Object.entries(categories).forEach(([categoryName, instances]) => {
    if (!instances || instances.length === 0) return;

    const categoryEl = document.createElement('div');
    categoryEl.className = 'category';

    const headerEl = document.createElement('div');
    headerEl.className = 'category-header';

    const titleEl = document.createElement('div');
    titleEl.className = 'category-title';
    titleEl.textContent = categoryName;

    const toggleEl = document.createElement('div');
    toggleEl.className = 'category-toggle';
    toggleEl.textContent = '▸';

    headerEl.appendChild(titleEl);
    headerEl.appendChild(toggleEl);
    categoryEl.appendChild(headerEl);

    const listWrapper = document.createElement('div');
    listWrapper.className = 'instance-list';

    const listInner = document.createElement('div');
    listInner.className = 'instance-list-inner';

    instances.forEach(inst => {
      // Backwards compatibility:
      // - if inst is a string => technicalName = inst, displayName = inst
      // - if inst is an object => technicalName/name fields
      let technicalName;
      let displayName;

      if (typeof inst === 'string') {
        technicalName = inst;
        displayName   = inst;
      } else {
        technicalName = inst.technicalName || inst.name;
        displayName   = inst.name || inst.technicalName;
      }

      if (!technicalName) return; // nothing to load

      const item = document.createElement('div');
      item.className = 'instance-item';
      item.textContent = displayName;  // <-- show human-readable name

      item.addEventListener('click', () => {
        loadInstance(categoryName, technicalName, displayName);
      });

      listInner.appendChild(item);
    });

    listWrapper.appendChild(listInner);
    categoryEl.appendChild(listWrapper);

    headerEl.addEventListener('click', () => {
      const expanded = categoryEl.classList.toggle('expanded');
      toggleEl.textContent = expanded ? '▾' : '▸';
    });

    categoryList.appendChild(categoryEl);
  });
}

// Clear all tiles or maybe only ones for same instance?
function clearTiles() {
  tileGrid.innerHTML = '';
}

// Core instance loader:
// 1) fetch data JSON
// 2) dynamic-import JS module
// 3) let module render into a tile
async function loadInstance(categoryName, technicalName, displayName) {
  const date = currentDate;
  if (!date) return;

  const dataUrl    = `data/${date}/${categoryName}/${technicalName}.json`;
  const moduleUrl  = new URL(`./${categoryName}/${technicalName}.js`, import.meta.url);

  try {
    const res = await fetch(dataUrl);
    if (!res.ok) {
      throw new Error(`Failed to load ${dataUrl}: ${res.status}`);
    }
    const data = await res.json();

    // Create tile container
    const tile = document.createElement('article');
    tile.className = 'tile';

    const header = document.createElement('div');
    header.className = 'tile-header';
    header.textContent = displayName || technicalName;

    const meta = document.createElement('div');
    meta.className = 'tile-meta';
    meta.textContent = `${categoryName} · ${date}`;

    const body = document.createElement('div');
    body.className = 'tile-body';

    tile.appendChild(header);
    tile.appendChild(meta);
    tile.appendChild(body);
    tileGrid.appendChild(tile);

    // Dynamically import the instance-specific module
    let mod;
    try {
      mod = await import(moduleUrl);
    } catch (e) {
      console.warn(`No module found at ${moduleUrl}; using fallback renderer.`);
    }

    if (mod && typeof mod.render === 'function') {
      mod.render(body, data, {
        date,
        category: categoryName,
        instance: technicalName,
        displayName: displayName || technicalName,
      });
    } else {
      // Fallback: raw JSON dump
      body.textContent = JSON.stringify(data, null, 2);
      body.style.whiteSpace = 'pre';
      body.style.fontFamily = 'monospace';
      body.style.fontSize = '0.75rem';
    }

  } catch (err) {
    console.error(err);
    alert(`Failed to load instance "${displayName || technicalName}": ${err.message}`);
  }
}

// Bootstrapping
async function init() {
  attachSortButtons();
  snapshotSelect.addEventListener('change', snapshotChanged);

  try {
    await fetchIndex();
    populateSnapshotSelect();
    rebuildCategories();
  } catch (err) {
    console.error(err);
    categoryList.textContent = 'Failed to load index.json.';
  }
}

init();