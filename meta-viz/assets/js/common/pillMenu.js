// assets/js/common/pillMenu.js

/**
 * Create a pill menu bar with expandable panels.
 *
 * Usage:
 *   const { bar, pills } = createPillMenu(parent, [
 *     { id: 'groups', label: 'Groups', build: ({ controlsEl, chipsEl }) => { ... } },
 *     { id: 'hubs',   label: 'Hubs',   build: ({ controlsEl, chipsEl }) => { ... } },
 *   ]);
 */
export function createPillMenu(parent, sections) {
  const bar = document.createElement('div');
  bar.className = 'dup-pill-bar';
  parent.appendChild(bar);

  const pills = new Map();

  sections.forEach(cfg => {
    const pill = document.createElement('div');
    pill.className = 'dup-pill';
    bar.appendChild(pill);

    // The little toggle button (always visible)
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'dup-pill-button';
    button.textContent = cfg.label;
    pill.appendChild(button);

    // The dropdown panel
    const panel = document.createElement('div');
    panel.className = 'dup-pill-panel';
    pill.appendChild(panel);

    // Header row inside the panel: title + controls
    const headerRow = document.createElement('div');
    headerRow.className = 'dup-pill-header-row';
    panel.appendChild(headerRow);

    const titleEl = document.createElement('div');
    titleEl.className = 'dup-pill-title';
    titleEl.textContent = cfg.label;
    headerRow.appendChild(titleEl);

    const controlsEl = document.createElement('div');
    controlsEl.className = 'dup-pill-controls';
    headerRow.appendChild(controlsEl);

    // Chip area (your group/hub/island chips)
    const chipShell = document.createElement('div');
    chipShell.className = 'app-header-chip-shell';
    panel.appendChild(chipShell);

    const chipsEl = document.createElement('div');
    chipsEl.className = 'app-header-chip-container';
    chipShell.appendChild(chipsEl);

    // Open/close behavior
    button.addEventListener('click', (e) => {
      e.stopPropagation();
      const isOpen = pill.classList.toggle('dup-pill-open');
      if (isOpen) {
        // Close other pills in this bar
        bar.querySelectorAll('.dup-pill').forEach(other => {
          if (other !== pill) other.classList.remove('dup-pill-open');
        });
      }
    });

    // Clicks inside the panel shouldn’t close it
    panel.addEventListener('click', (e) => {
      e.stopPropagation();
    });

    // Allow caller to populate controls + chips
    if (typeof cfg.build === 'function') {
      cfg.build({ pill, button, panel, headerRow, titleEl, controlsEl, chipsEl });
    }

    pills.set(cfg.id, { pill, button, panel, headerRow, titleEl, controlsEl, chipsEl });
  });

  // Clicking outside closes all pills in this bar
  document.addEventListener('click', (e) => {
    if (!bar.contains(e.target)) {
      bar.querySelectorAll('.dup-pill').forEach(p => p.classList.remove('dup-pill-open'));
    }
  });

  return { bar, pills };
}