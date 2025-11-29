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
    duplicate: {
      color: '#666666',
      arrow: false,
      widthTiles: 0.03,
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
  const cfg = EDGE_STYLE_CONFIG;

  return (edge) => {
    const relConf  = edge.relName && cfg.relationTypes[edge.relName];
    const kindConf = cfg.kinds[edge.kind];

    const base = relConf || kindConf || cfg.fallback;

    return {
      color: base.color,
      arrow: base.arrow ?? true,
      widthTiles: base.widthTiles,
      headTiles:  base.headTiles,
      offsetFraction: base.offsetFraction,
    };
  };
}