// assets/js/common/physicsPanel.js
import {
  REPULSION_DEFAULTS,
  CENTER_FORCE,
  SPRING_DEFAULTS,
  DAMPING,
  SPRING_DAMPING,
  FRICTION,
} from './physicsParams.js';

import {
  createToggleRow,
  createSliderRow,
} from './uiControls.js';

/**
 * Attach a floating physics panel to a container.
 *
 * @param {Object} opts
 *   parent:  HTMLElement to attach overlay into (e.g. graphPanel)
 *   physics: PhysicsSystem instance (for timeScale slider)
 *   title:   Optional title for the panel
 */
export function attachPhysicsPanel({ parent, physics, title = 'Physics controls' }) {
  if (!parent || !physics) {
    console.warn('[attachPhysicsPanel] Missing parent or physics');
    return;
  }

  // Avoid duplicating if someone calls twice on the same parent
  if (parent.querySelector('.physics-overlay')) {
    return;
  }

  // Small overlay anchored to the graph area
  const overlay = document.createElement('div');
  overlay.className = 'physics-overlay';
  parent.appendChild(overlay);

  // Tiny handle (always visible)
  const handle = document.createElement('div');
  handle.className = 'physics-handle';
  handle.title = 'Physics controls';
  handle.textContent = '⚙'; // change if you want
  overlay.appendChild(handle);

  // Expanded panel with actual controls
  const panel = document.createElement('div');
  panel.className = 'physics-panel';
  overlay.appendChild(panel);

  const titleEl = document.createElement('div');
  titleEl.className = 'physics-panel-title';
  titleEl.textContent = title;
  panel.appendChild(titleEl);

  const physicsContainer = document.createElement('div');
  physicsContainer.className = 'physics-container';
  panel.appendChild(physicsContainer);

  // Click-to-toggle (for mobile / keyboard users)
  handle.addEventListener('click', (e) => {
    e.stopPropagation();
    overlay.classList.toggle('physics-open');
  });

  // Clicking anywhere else closes it
  document.addEventListener('click', (e) => {
    if (!overlay.contains(e.target)) {
      overlay.classList.remove('physics-open');
    }
  });

  panel.addEventListener('click', (e) => {
    e.stopPropagation();
  });

  // ---- Controls ----

  // toggles
  createToggleRow(physicsContainer, 'Repulsion', REPULSION_DEFAULTS.enabled, (val) => {
    REPULSION_DEFAULTS.enabled = val;
  });

  createToggleRow(physicsContainer, 'Center pull', CENTER_FORCE.enabled, (val) => {
    CENTER_FORCE.enabled = val;
  });

  // sliders
  createSliderRow(physicsContainer, {
    labelText: 'Spring k',
    min: 0,
    max: 50,
    step: 1,
    initial: SPRING_DEFAULTS.k,
    decimals: 1,
    onChange: (v) => { SPRING_DEFAULTS.k = v; },
  });

  createSliderRow(physicsContainer, {
    labelText: 'Spring rest factor',
    min: 0.1,
    max: 2.0,
    step: 0.05,
    initial: SPRING_DEFAULTS.restFactor,
    decimals: 2,
    onChange: (v) => { SPRING_DEFAULTS.restFactor = v; },
  });

  createSliderRow(physicsContainer, {
    labelText: 'Repulsion k',
    min: 0,
    max: 30,
    step: 0.5,
    initial: REPULSION_DEFAULTS.k,
    decimals: 1,
    onChange: (v) => { REPULSION_DEFAULTS.k = v; },
  });

  createSliderRow(physicsContainer, {
    labelText: 'Repulsion buffer',
    min: 1,
    max: 10,
    step: 0.5,
    initial: REPULSION_DEFAULTS.buffer,
    decimals: 1,
    onChange: (v) => { REPULSION_DEFAULTS.buffer = v; },
  });

  createSliderRow(physicsContainer, {
    labelText: 'Repulsion scale',
    min: 0,
    max: 5,
    step: 0.1,
    initial: REPULSION_DEFAULTS.scale ?? 1.0,
    decimals: 2,
    onChange: (v) => { REPULSION_DEFAULTS.scale = v; },
  });

  createSliderRow(physicsContainer, {
    labelText: 'Center k',
    min: 0,
    max: 10.0,
    step: 0.02,
    initial: CENTER_FORCE.k,
    decimals: 2,
    onChange: (v) => { CENTER_FORCE.k = v; },
  });

  createSliderRow(physicsContainer, {
    labelText: 'Global damp.',
    min: 0,
    max: 10.0,
    step: 0.05,
    initial: 2.0,
    decimals: 2,
    onChange: (v) => { DAMPING.gamma = v; },
  });

  createSliderRow(physicsContainer, {
    labelText: 'Spring damp.',
    min: 0,
    max: 10.0,
    step: 0.05,
    initial: 5.0,
    decimals: 2,
    onChange: (v) => { SPRING_DAMPING.gamma = v; },
  });

  createSliderRow(physicsContainer, {
    labelText: 'Friction mu.',
    min: 0.1,
    max: 3.0,
    step: 0.05,
    initial: 1.0,
    decimals: 2,
    onChange: (v) => { FRICTION.muK = v; },
  });

  createSliderRow(physicsContainer, {
    labelText: 'Time scale',
    min: 0.1,
    max: 8.0,
    step: 0.05,
    initial: 5.0,
    decimals: 2,
    onChange: (v) => { physics.timeScale = v; },
  });

  return { overlay, handle, panel, physicsContainer };
}