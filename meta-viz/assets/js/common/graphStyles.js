// ---- global theme (colors etc.) -----------------------------------

import { ICON_WORLD_SIZE, getNodeScale } from './spriteParams.js';

// re-export so existing imports keep working
export { ICON_WORLD_SIZE };

export function makeMetaisNodeScale() {
  return (node) => getNodeScale(node);
}

export const DARK_GRAPH_THEME = {
  backgroundColor: '#050505',
  showGrid: true,
  gridMinorColor: '#3b2c20',
  gridMajorColor: '#5c4a32',
  axesColor:      '#888',
};

// ---- per-relation / per-kind edge styles ---------------------------
//
// All units that end with "Tiles" are fractions of grid tile size.
// (grid tile size in px is just `zoom` in graph.js)

const EDGE_STYLE_CONFIG = {
  // Specific relation types (edge.relName)
  relationTypes: {
    PO_je_gestor_KS: {
      color: '#b0413e',
      arrow: true,
      widthTiles: 0.10,
      headTiles:  0.50,
      offsetFraction: 0.25,
    },
    PO_je_spravca_ISVS: {
      color: '#2ce4d4ff',
      arrow: true,
      widthTiles: 0.10,
      headTiles:  0.50,
      offsetFraction: 0.25,
    },
    PO_je_podriadenou_PO: {
      color: '#e42cb6ff',
      arrow: true,
      widthTiles: 0.10,
      headTiles:  0.50,
      offsetFraction: 0.25,
    },
    PO_asociuje_Projekt: {
      color: '#2d497eff',
      arrow: true,
      widthTiles: 0.06,
      headTiles:  0.40,
      offsetFraction: 0.25,
    },
    ZC_ma_gestora_PO: {
      color: '#66aac5ff',
      arrow: true,
      widthTiles: 0.10,
      headTiles:  0.50,
      offsetFraction: 0.25,
    },
    KRIS_stanovuje_CIEL: {
      color: '#e6a26aff',
      arrow: true,
      widthTiles: 0.06,
      headTiles:  0.40,
      offsetFraction: 0.25,
    },
    has_similar_name: {
      color: '#ffffffff',
      arrow: true,
      widthTiles: 0.15,
      headTiles:  0.50,
      offsetFraction: 0.15,
    },
    share_same_metaid: {
      color: '#666666',
      arrow: false,
      widthTiles: 0.03,
      offsetFraction: 0.1,
    },
  },

  // Generic per "kind" fallback (edge.kind)
  kinds: {
    relation: {
      color: '#e4b84a',
      arrow: true,
      widthTiles: 0.06,
      headTiles:  0.40,
      offsetFraction: 0.2,
    },
  },

  // ultimate fallback
  fallback: {
    color: '#444444',
    arrow: false,
    widthTiles: 0.04,
  },
};

// Factory that turns the config into a getEdgeStyle function
export function makeMetaisEdgeStyle() {
  return (edge, n1, n2) => {
    // start from your config (colors, arrow, widths, etc.)
    const relCfg  = EDGE_STYLE_CONFIG.relationTypes[edge.relName];
    const kindCfg = EDGE_STYLE_CONFIG.kinds[edge.kind];
    const baseCfg = relCfg || kindCfg || EDGE_STYLE_CONFIG.fallback;

    const baseStyle = {
      color:          baseCfg.color,
      arrow:          baseCfg.arrow ?? false,
      widthTiles:     baseCfg.widthTiles ?? 0.04,
      headTiles:      baseCfg.headTiles,
      offsetFraction: baseCfg.offsetFraction,
    };

    // distance-based styling for relation edges
    let d = edge.distance;
    if (!Number.isFinite(d) || d < 0) d = Infinity;

    let alpha = 0.9;
    let dash  = null;

    if (d === 0) {
      // strongest
      alpha = 1.0;
      dash  = null;
    } else if (d === 1) {
      // dashed
      alpha = 0.5;
      dash  = [6, 4];
    } else if (d === 2) {
      // dotted-ish
      alpha = 0.35;
      dash  = [2, 4];
    } else {
      // unreachable / deep distance
      alpha = 0.2;
      dash  = [1, 6];
    }

    return {
      ...baseStyle,
      alpha,
      dash,
    };
  };
}