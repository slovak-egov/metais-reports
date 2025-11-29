// Base sprite size in world units (grid tiles)
export const ICON_WORLD_SIZE = 2.5;

// Per-type scale multiplier (same idea as your NODE_SCALE_CONFIG)
const NODE_SCALE_CONFIG = {
  PO:      1.3,
  default: 1.0,
};

export function getNodeScale(node) {
  const t = node?.type;
  if (!t) return NODE_SCALE_CONFIG.default;
  return NODE_SCALE_CONFIG[t] ?? NODE_SCALE_CONFIG.default;
}

/**
 * World-radius of a node, based on ICON_WORLD_SIZE and per-type scale.
 * (Physics code uses this for springs / repulsion, render code uses it for sizing.)
 */
export function getNodeRadiusWorld(node) {
  const scale = getNodeScale(node);
  // ICON_WORLD_SIZE is roughly “diameter” in world units → radius = 0.5 * scale * ICON_WORLD_SIZE
  return 0.5 * ICON_WORLD_SIZE * scale;
}