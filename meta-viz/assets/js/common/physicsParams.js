// meta-viz/assets/js/common/physicsParams.js

import { getNodeRadiusWorld } from './spriteParams.js';

// --- Mass configuration -------------------------------------------------

export const MASS_CONFIG = {
  default: 1.0,
  PO:      25.0,   // POs heavier so they move less / pull more
};

export function getNodeMass(node) {
  const t = node?.type;
  if (!t) return MASS_CONFIG.default;
  return MASS_CONFIG[t] ?? MASS_CONFIG.default;
}

// --- Spring configuration for relations --------------------------------
//
// For an edge (relation), we compute:
//   - k_spring: stiffness
//   - restLength: rest distance in world units

export const SPRING_DEFAULTS = {
  k: 2.0,          // base spring stiffness
  restFactor: 1.0, // restLength ≈ restFactor * (r1 + r2)
};

export const SPRING_PER_RELATION = {
  // Examples:
  "Has same MetaIS code": {
    k: 8.0, 
    restFactor: 0.5
  },
};

export function getSpringParams(edge, n1, n2) {
  const base = SPRING_DEFAULTS;
  const override = SPRING_PER_RELATION[edge.relName] || {};

  const r1 = getNodeRadiusWorld(n1);
  const r2 = getNodeRadiusWorld(n2);
  const restFactor = override.restFactor ?? base.restFactor;
  const restLength = restFactor * (r1 + r2);

  const k = override.k ?? base.k;

  return { k, restLength };
}

// --- Repulsion (anti-overlap) ------------------------------------------
//
// When distance < repelDistance, push them apart with
//   F ≈ k_rep * (repelDistance - d)

export const REPULSION_DEFAULTS = {
  enabled: true,
  buffer : 5.0,   // multiplier on “size” threshold
  k      : 8.0,   // strength
  scale  : 1.0
};

export function getRepulsionParams(n1, n2) {
  const { buffer, k, scale = 1.0 } = REPULSION_DEFAULTS;

  const r1 = getNodeRadiusWorld(n1);
  const r2 = getNodeRadiusWorld(n2);
  const maxR = Math.max(r1, r2);

  const repelDistance = buffer * maxR; // threshold for starting repulsion
  const effectiveK    = k * scale;

  return { k: effectiveK, repelDistance };
}

// --- Global center pull -------------------------------------------------

export const CENTER_FORCE = {
  enabled: true,
  k:       0.3,   // F_center = -k * r (world units)
};

// --- Damping ------------------------------------------------------------
//
// Simple velocity damping: F_damp = -gamma * v

export const DAMPING = {
  gamma: 1.0,
};