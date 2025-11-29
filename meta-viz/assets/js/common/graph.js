const SPRITE_MIN_PX = 8;    // smallest rasterized size
const SPRITE_MAX_PX = 1024;  // hard cap; beyond this we just reuse this size
const SPRITE_QUANT_RATIO = 2; // must be > 1

/*
function quantizeIconSizePx(iconSize) {
    let key = SPRITE_MIN_PX;

    // multiply upwards until >= iconSize
    while (key < iconSize) {
        key *= SPRITE_QUANT_RATIO;
    }

    if (key > SPRITE_MAX_PX) {
      key = SPRITE_MAX_PX;
    }

    return Math.round(key);
}*/

const SPRITE_SIZES = [128, 512];

function quantizeIconSizePx(iconSize) {
  const desired = Math.max(SPRITE_SIZES[0], Math.min(iconSize, SPRITE_SIZES[SPRITE_SIZES.length - 1]));

  let best = SPRITE_SIZES[0];
  let bestDiff = Math.abs(best - desired);

  for (let i = 1; i < SPRITE_SIZES.length; i++) {
    const s = SPRITE_SIZES[i];
    const d = Math.abs(s - desired);
    if (d < bestDiff) {
      bestDiff = d;
      best = s;
    }
  }
  return best;
}

function resolveSpriteType(node) {
  const t = node.type || '';

  // You can tweak this logic depending on your citypes naming
  if (/system/i.test(t) || t.startsWith('CMDB_')) {
    return 'default_system';
  }

  if (!t) {
    // no type → treat as application
    return 'default_application';
  }

  // If you have specific icons for known types, keep using them:
  // if (t === 'KS')   return 'KS';
  // if (t === 'ISVS') return 'ISVS';
  // ...

  // unknown type → generic application-ish fallback
  return 'default_application';
}

function rasterizeSVGToCanvas(img, size) {
  const off = document.createElement('canvas');
  off.width = size;
  off.height = size;
  const ctx = off.getContext('2d');
  ctx.drawImage(img, 0, 0, size, size);
  return off;
}

/**
 * Default arrowhead renderer: simple filled triangle.
 * tipX, tipY = arrow tip (where the arrow should end)
 * angle       = direction of the arrow (radians, from start -> end)
 * size        = arrowhead length in pixels
 */
export function defaultArrowHead(ctx, tipX, tipY, angle, size, options = {}) {
  const { wingFrac = 0.5 } = options;

  const backX = tipX - Math.cos(angle) * size;
  const backY = tipY - Math.sin(angle) * size;

  const wing = size * wingFrac; // half width of the base

  const leftX  = backX + Math.cos(angle + Math.PI / 2) * wing;
  const leftY  = backY + Math.sin(angle + Math.PI / 2) * wing;
  const rightX = backX + Math.cos(angle - Math.PI / 2) * wing;
  const rightY = backY + Math.sin(angle - Math.PI / 2) * wing;

  ctx.beginPath();
  ctx.moveTo(tipX, tipY);
  ctx.lineTo(leftX, leftY);
  ctx.lineTo(rightX, rightY);
  ctx.closePath();
  ctx.fill();
}

export function drawArrow(ctx, x1, y1, x2, y2, options = {}) {
  const {
    spriteSize1    = 0,
    spriteSize2    = 0,
    offsetFraction = 0.1,
    headSize       = 10,
    headRenderer   = defaultArrowHead,
    edge           = null,
  } = options;

  const dx = x2 - x1;
  const dy = y2 - y1;
  const len = Math.hypot(dx, dy) || 1;
  const ux = dx / len;
  const uy = dy / len;

  // Radii (assuming roughly square sprites)
  const r1 = spriteSize1 * 0.5;
  const r2 = spriteSize2 * 0.5;

  const extra1 = r1 * offsetFraction;
  const extra2 = r2 * offsetFraction;

  // Where the visible arrow should START (just outside source sprite)
  const startX = x1 + ux * (r1 + extra1);
  const startY = y1 + uy * (r1 + extra1);

  // Where the arrow TIP should end (just before target sprite edge)
  const tipX = x2 - ux * (r2 + extra2);
  const tipY = y2 - uy * (r2 + extra2);

  const totalVisibleLen = Math.hypot(tipX - startX, tipY - startY);

  // If there's no room for an arrowhead, just draw a simple line
  const effectiveHead = Math.min(headSize, totalVisibleLen * 0.5);
  const lineLen = Math.max(0, totalVisibleLen - effectiveHead);

  const lineEndX = startX + ux * lineLen;
  const lineEndY = startY + uy * lineLen;

  // Draw the shaft
  ctx.beginPath();
  ctx.moveTo(startX, startY);
  ctx.lineTo(lineEndX, lineEndY);
  ctx.stroke();

  // Direction angle
  const angle = Math.atan2(dy, dx);

  // Draw the head
  if (effectiveHead > 0 && headRenderer) {
    headRenderer(ctx, tipX, tipY, angle, effectiveHead, edge);
  }
}

function shortenLine(x1, y1, x2, y2, cut1, cut2) {
  const dx = x2 - x1;
  const dy = y2 - y1;
  const len = Math.hypot(dx, dy) || 1;
  const ux = dx / len;
  const uy = dy / len;

  const sx = x1 + ux * cut1;
  const sy = y1 + uy * cut1;
  const ex = x2 - ux * cut2;
  const ey = y2 - uy * cut2;

  return { sx, sy, ex, ey };
}

export class GraphViewport {
  constructor(rootEl, options = {}) {
    this.rootEl = rootEl;

    // --- defaults ---
    this.options = {
      backgroundColor: '#050505',
      showGrid:        true,
      gridMinorColor:  '#3b2c20',
      gridMajorColor:  '#5c4a32',
      axesColor:       '#888',
      initialZoom:     20,
      minZoom:         1,
      maxZoom:         1000,
      debug:           false,
      canvasClass:     'graph-canvas',
      debugClass:      'graph-debug',
      iconWorldSize:   1.0,
    
      // styling callbacks
      getNodeSpriteType: node => node.type || 'UNKNOWN',
      getNodeScale:      node => 1.0,
      getEdgeStyle: edge => ({
        color: edge.kind === 'relation' ? '#e4b84a' : '#444',
        width: edge.kind === 'relation' ? 1.5 : 1,
        arrow: edge.kind === 'relation',
      }),
    
      arrowHeadRenderer: null,

      getNodeGlow: null,

      // interaction callbacks
      onNodeHover:       null,
      onNodeHoverEnd:    null,
      onNodeClick:       null,
      enableNodeDragging: false,
    
      onEdgeHover:    null,
      onEdgeHoverEnd: null,
    
      onAfterDraw: null,
    
      ...options,
    };

    // --- DOM ---
    this.canvas = document.createElement('canvas');
    this.canvas.className = this.options.canvasClass;
    this.rootEl.appendChild(this.canvas);

    this.ctx = this.canvas.getContext('2d');

    this.debugEl = document.createElement('div');
    this.debugEl.className = this.options.debugClass;
    if (this.options.debug) {
      this.rootEl.appendChild(this.debugEl);
    }

    // --- state ---
    this.devicePixelRatioUsed = 1;
    this.offsetX = 0;
    this.offsetY = 0;
    this.zoom    = this.options.initialZoom;
    this.minZoom = this.options.minZoom;
    this.maxZoom = this.options.maxZoom;

    this.logicalWidth  = 0;
    this.logicalHeight = 0;

    this.scene = { nodes: [], edges: [] };
    this.nodeById = new Map();

    // sprite cache: type → { svgImg, bitmapMap, loaded }
    this.spriteCache = new Map();
    this._spriteJobQueue = []; // job queue for sprites
    this._spriteJobScheduled = false;

    this.hoveredNode = null;
    this.hoveredEdge = null;
    this.isDragging  = false;
    this.lastX = 0;
    this.lastY = 0;

    this._installEvents();

    // resize observers
    const ro = new ResizeObserver(() => this.resize());
    ro.observe(this.rootEl);
    window.addEventListener('resize', () => this.resize());

    requestAnimationFrame(() => this.resize());
  }

  // ---------- public API ----------

  setScene(scene) {
    this.scene = scene || { nodes: [], edges: [] };
    this.nodeById.clear();
    for (const n of this.scene.nodes || []) {
      if (n.id != null) this.nodeById.set(n.id, n);
    }
    this.draw();
  }

  // ---------- sprite loading ----------

  _getSprite(type) {
    let record = this.spriteCache.get(type);
    if (record) return record;

    const svgImg = new Image();
    const bitmapMap = new Map(); // size(px) → canvas
    const pending = new Set();

    record = { svgImg, bitmapMap, loaded: false, pending };
    this.spriteCache.set(type, record);

    svgImg.onload = () => {
      record.loaded = true;

      // seed a small base sprite so we always have *something* sharper than a circle
      const baseSize = 32;
      if (!bitmapMap.has(baseSize)) {
        const baseCanvas = rasterizeSVGToCanvas(svgImg, baseSize);
        bitmapMap.set(baseSize, baseCanvas);
      }

      this.draw();
    };

    svgImg.onerror = () => {
      console.warn(`Failed to load sprite ${type}, trying fallback default_application.`);
      if (!svgImg.src.endsWith('default_application.svg')) {
        svgImg.src = 'sprites/default_application.svg';
      }
    };

    svgImg.src = `sprites/${type}.svg`;
    return record;
  }

  _scheduleSpriteRaster(type, sizeKey) {
    const record = this._getSprite(type);
    if (!record.loaded) return;
    if (record.bitmapMap.has(sizeKey)) return;
    if (record.pending.has(sizeKey)) return;

    record.pending.add(sizeKey);
    this._spriteJobQueue.push({ type, sizeKey });

    if (!this._spriteJobScheduled) {
      this._spriteJobScheduled = true;

      const runner = (deadline) => {
        this._processSpriteJobs(deadline);
      };

      if (window.requestIdleCallback) {
        window.requestIdleCallback(runner);
      } else {
        setTimeout(() => runner(null), 0);
      }
    }
  }

  _processSpriteJobs(deadline) {
    const timeBudget = (deadline && typeof deadline.timeRemaining === 'function')
      ? () => deadline.timeRemaining() > 4
      : () => true; // no info, just do a small fixed batch

    // Limit how many jobs per chunk to avoid jank
    let jobsPerChunk = 3;

    while (this._spriteJobQueue.length && jobsPerChunk > 0 && timeBudget()) {
      const job = this._spriteJobQueue.shift();
      jobsPerChunk--;

      if (!job) break;
      const { type, sizeKey } = job;
      const record = this.spriteCache.get(type);
      if (!record || !record.loaded) continue;

      // already done?
      if (record.bitmapMap.has(sizeKey)) continue;

      // do the expensive-ish rasterization
      const canvas = rasterizeSVGToCanvas(record.svgImg, sizeKey);
      record.bitmapMap.set(sizeKey, canvas);
      record.pending.delete(sizeKey);
    }

    this._spriteJobScheduled = false;

    // If more work remains, schedule another idle slice
    if (this._spriteJobQueue.length > 0) {
      const runner = (dl) => this._processSpriteJobs(dl);
      if (window.requestIdleCallback) {
        window.requestIdleCallback(runner);
      } else {
        setTimeout(() => runner(null), 0);
      }
    }

    // After updating bitmaps, redraw to pick up sharper sprites
    //this.draw();
  }

  _getClosestBitmap(record, desiredSize) {
    let bestCanvas = null;
    let bestSize   = 0;
    let bestDiff   = Infinity;

    for (const [size, canvas] of record.bitmapMap.entries()) {
      const diff = Math.abs(size - desiredSize);
      if (diff < bestDiff) {
        bestDiff   = diff;
        bestCanvas = canvas;
        bestSize   = size;
      }
    }

    if (!bestCanvas) return null;
    return { canvas: bestCanvas, size: bestSize };
  }

  _hitTestNode(screenX, screenY) {
    const nodes = this.scene.nodes || [];
    if (!nodes.length) return null;

    const w = this.logicalWidth;
    const h = this.logicalHeight;
    const baseSize = this.options.iconWorldSize * this.zoom;

    let best = null;
    let bestDist2 = Infinity;

    for (const node of nodes) {
      const sx = w / 2 + this.offsetX + this.zoom * node.x;
      const sy = h / 2 + this.offsetY + this.zoom * node.y;

      const scale  = this.options.getNodeScale(node) ?? 1;
      const radius = (baseSize * scale) * 0.5; // approximate sprite radius

      const dx = screenX - sx;
      const dy = screenY - sy;
      const dist2 = dx * dx + dy * dy;

      if (dist2 <= radius * radius && dist2 < bestDist2) {
        bestDist2 = dist2;
        best = node;
      }
    }

    return best;
  }

  _hitTestEdge(screenX, screenY) {
    const edges = this.scene.edges || [];
    if (!edges.length) return null;

    const w = this.logicalWidth;
    const h = this.logicalHeight;
    const tilePx = this.zoom;
    const iconWorldSize = this.options.iconWorldSize;

    // distance from point to segment
    function distPointToSegment(px, py, x1, y1, x2, y2) {
      const dx = x2 - x1;
      const dy = y2 - y1;
      const len2 = dx*dx + dy*dy;
      if (len2 === 0) return Math.hypot(px - x1, py - y1);
      const t = Math.max(0, Math.min(1, ((px - x1)*dx + (py - y1)*dy) / len2));
      const projX = x1 + t * dx;
      const projY = y1 + t * dy;
      return Math.hypot(px - projX, py - projY);
    }

    let best = null;
    let bestDist = Infinity;

    for (const edge of edges) {
      const src = this.nodeById.get(edge.source);
      const dst = this.nodeById.get(edge.target);
      if (!src || !dst) continue;

      const ax = w / 2 + this.offsetX + this.zoom * src.x;
      const ay = h / 2 + this.offsetY + this.zoom * src.y;
      const bx = w / 2 + this.offsetX + this.zoom * dst.x;
      const by = h / 2 + this.offsetY + this.zoom * dst.y;

      // approximate visible line using same idea as drawArrow/shortenLine
      const scaleSrc = this.options.getNodeScale(src) ?? 1;
      const scaleDst = this.options.getNodeScale(dst) ?? 1;
      const iconSize1 = iconWorldSize * this.zoom * scaleSrc;
      const iconSize2 = iconWorldSize * this.zoom * scaleDst;

      const startCut = iconWorldSize * this.zoom * scaleSrc * 0.6;
      const endCut   = iconWorldSize * this.zoom * scaleDst * 0.6;

      // shorten line
      const dx = bx - ax;
      const dy = by - ay;
      const len = Math.hypot(dx, dy) || 1;
      const ux = dx / len;
      const uy = dy / len;

      const sx = ax + ux * startCut;
      const sy = ay + uy * startCut;
      const ex = bx - ux * endCut;
      const ey = by - uy * endCut;

      const style = this.options.getEdgeStyle(edge) || {};
      const widthTiles = style.widthTiles;
      const widthPx = (widthTiles != null ? widthTiles * tilePx : (style.width ?? 1));

      // pick hover tolerance in px
      const tolerance = Math.max(6, widthPx * 1.5);

      const d = distPointToSegment(screenX, screenY, sx, sy, ex, ey);
      if (d <= tolerance && d < bestDist) {
        bestDist = d;
        best = edge;
      }
    }

    return best;
  }

  // ---------- layout / drawing ----------

  worldToScreen(x, y) {
    const w = this.logicalWidth;
    const h = this.logicalHeight;
    const sx = w / 2 + this.offsetX + this.zoom * x;
    const sy = h / 2 + this.offsetY + this.zoom * y;
    return { x: sx, y: sy };
  }

  resize() {
    const dpr = window.devicePixelRatio || 1;
    this.devicePixelRatioUsed = dpr;

    // Use the container (graph-root), not the canvas itself
    const rect = this.rootEl.getBoundingClientRect();
    const w = rect.width;
    const h = rect.height;
    if (!w || !h) return;

    this.logicalWidth  = w;
    this.logicalHeight = h;

    this.canvas.width  = w * dpr;
    this.canvas.height = h * dpr;

    // Make sure CSS size matches the logical one
    this.canvas.style.width  = `${w}px`;
    this.canvas.style.height = `${h}px`;

    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.ctx.lineWidth = 1 / dpr;

    this.draw();
  }

  draw() {
    const g = this.ctx;
    const w = this.logicalWidth;
    const h = this.logicalHeight;
    if (!w || !h) return;

    // background
    g.clearRect(0, 0, w, h);
    g.fillStyle = this.options.backgroundColor;
    g.fillRect(0, 0, w, h);

    const cx = w / 2 + this.offsetX;
    const cy = h / 2 + this.offsetY;

    // --- GRID in world coords ---
    if (this.options.showGrid) {
      const targetMinPx = 6;   // we want smallest grid step ≥ ~6 px
      const targetMaxPx = 60;  // (kept from your original, even if unused)

      // pixels per world unit
      const pxPerWorld = this.zoom;

      if (pxPerWorld > 0) {
        // Ideal *world* step that would give us ~targetMinPx spacing
        const idealWorldStep = targetMinPx / pxPerWorld;

        // Snap that ideal step to a power of 10: 0.001, 0.01, 0.1, 1, 10, ...
        const exponent = Math.floor(Math.log10(idealWorldStep));
        const baseStepWorld = Math.pow(10, exponent);

        // Define 3 levels:
        //   fine   = base
        //   medium = base * 10
        //   coarse = base * 100
        const levels = [
          {
            stepWorld: baseStepWorld,
            alpha:     0.25,
            widthPx:   1,
          },
          {
            stepWorld: baseStepWorld * 10,
            alpha:     0.40,
            widthPx:   1.5,
          },
          {
            stepWorld: baseStepWorld * 100,
            alpha:     0.65,
            widthPx:   2,
          },
        ];

        const worldXMin = (0   - w / 2 - this.offsetX) / this.zoom;
        const worldXMax = (w   - w / 2 - this.offsetX) / this.zoom;
        const worldYMin = (0   - h / 2 - this.offsetY) / this.zoom;
        const worldYMax = (h   - h / 2 - this.offsetY) / this.zoom;

        for (const level of levels) {
          const { stepWorld, alpha, widthPx } = level;

          // Actual pixel spacing for this level
          const stepPx = stepWorld * pxPerWorld;
          if (stepPx < 1) {
            // too dense to bother
            continue;
          }

          // Soft clamp to avoid crazy amounts of lines when zoomed way out/in
          if (stepPx < 3 && stepWorld === baseStepWorld) {
            // if even the "base" is super dense, you might skip it
            continue;
          }

          // Horizontal & vertical lines that fall in the current view
          const xStart = Math.floor(worldXMin / stepWorld) * stepWorld;
          const xEnd   = Math.ceil(worldXMax  / stepWorld) * stepWorld;
          const yStart = Math.floor(worldYMin / stepWorld) * stepWorld;
          const yEnd   = Math.ceil(worldYMax  / stepWorld) * stepWorld;

          // one color from theme, only alpha changes per level
          const color = this.options.gridMinorColor || '#3b2c20';

          g.strokeStyle = color;
          g.globalAlpha = alpha;
          g.lineWidth   = widthPx / this.devicePixelRatioUsed;

          // verticals
          for (let x = xStart; x <= xEnd; x += stepWorld) {
            const xScreen = w / 2 + this.offsetX + this.zoom * x;
            g.beginPath();
            g.moveTo(xScreen, 0);
            g.lineTo(xScreen, h);
            g.stroke();
          }

          // horizontals
          for (let y = yStart; y <= yEnd; y += stepWorld) {
            const yScreen = h / 2 + this.offsetY + this.zoom * y;
            g.beginPath();
            g.moveTo(0, yScreen);
            g.lineTo(w, yScreen);
            g.stroke();
          }

          g.globalAlpha = 1.0; // reset for other drawing
        }
      }
    }

    // --- axes ---
    g.strokeStyle = this.options.axesColor;
    g.lineWidth   = 1 / this.devicePixelRatioUsed;

    g.beginPath();
    g.moveTo(cx, 0);
    g.lineTo(cx, h);
    g.stroke();

    g.beginPath();
    g.moveTo(0, cy);
    g.lineTo(w, cy);
    g.stroke();

    // --- SCENE ---------------------------------------------------------
    const nodes = this.scene.nodes || [];
    const edges = this.scene.edges || [];

    const iconWorldSize = this.options.iconWorldSize;

    // ==============================================================
    // PASS 1: underglow (BELOW edges & sprites)
    // ==============================================================
    if (typeof this.options.getNodeGlow === 'function') {
      nodes.forEach(node => {
        const glow = this.options.getNodeGlow(node);
        if (!glow || !glow.color) return;

        const sx = w / 2 + this.offsetX + this.zoom * node.x;
        const sy = h / 2 + this.offsetY + this.zoom * node.y;

        const scale    = this.options.getNodeScale(node) ?? 1;
        const iconSize = iconWorldSize * this.zoom * scale;

        const radiusFactor = glow.radiusFactor ?? 1.8;
        const alpha        = glow.alpha ?? 0.35;
        const softness     = glow.softness ?? 0.8; // 0 = hard, 1 = very soft

        const rGlow  = 0.5 * iconSize * radiusFactor;
        const ctx    = this.ctx;
        const oldA   = ctx.globalAlpha;

        const innerStop = Math.max(0, Math.min(1, 1 - softness));

        const grad = ctx.createRadialGradient(sx, sy, 0, sx, sy, rGlow);
        grad.addColorStop(0, glow.color);
        grad.addColorStop(innerStop, glow.color);
        grad.addColorStop(1, 'rgba(0,0,0,0)');

        ctx.globalAlpha = alpha;
        ctx.fillStyle   = grad;
        ctx.beginPath();
        ctx.arc(sx, sy, rGlow, 0, 2 * Math.PI);
        ctx.fill();

        ctx.globalAlpha = oldA;
      });
    }

    // ==============================================================
    // PASS 2: edges
    // ==============================================================
    edges.forEach(edge => {
      const src = this.nodeById.get(edge.source);
      const dst = this.nodeById.get(edge.target);
      if (!src || !dst) return;

      const ax = w / 2 + this.offsetX + this.zoom * src.x;
      const ay = h / 2 + this.offsetY + this.zoom * src.y;
      const bx = w / 2 + this.offsetX + this.zoom * dst.x;
      const by = h / 2 + this.offsetY + this.zoom * dst.y;

      const style = this.options.getEdgeStyle(edge) || {};
      const color = style.color || '#444';
      const arrow = !!style.arrow;

      const tilePx = this.zoom;

      const widthTiles = style.widthTiles;
      const widthPx = (widthTiles != null ? widthTiles * tilePx : (style.width ?? 1));

      g.strokeStyle = color;
      g.fillStyle   = color;
      g.lineWidth   = widthPx / this.devicePixelRatioUsed;

      const scaleSrc = this.options.getNodeScale(src) ?? 1;
      const scaleDst = this.options.getNodeScale(dst) ?? 1;

      const iconSize1 = iconWorldSize * this.zoom * scaleSrc;
      const iconSize2 = iconWorldSize * this.zoom * scaleDst;

      if (arrow) {
        const headRenderer   = this.options.arrowHeadRenderer || defaultArrowHead;
        const offsetFraction = style.offsetFraction ?? 0.2;

        const headTiles = style.headTiles;
        const baseHead  = headTiles != null
          ? headTiles * tilePx
          : iconWorldSize * this.zoom * 0.8;

        drawArrow(g, ax, ay, bx, by, {
          spriteSize1:    iconSize1,
          spriteSize2:    iconSize2,
          offsetFraction,
          headSize:       baseHead,
          headRenderer,
          edge,
        });
      } else {
        const startCut = iconWorldSize * this.zoom * scaleSrc * 0.6;
        const endCut   = iconWorldSize * this.zoom * scaleDst * 0.6;
        const clipped  = shortenLine(ax, ay, bx, by, startCut, endCut);

        g.beginPath();
        g.moveTo(clipped.sx, clipped.sy);
        g.lineTo(clipped.ex, clipped.ey);
        g.stroke();
      }
    });

    // ==============================================================
    // PASS 3: node sprites (TOP)
    // ==============================================================
    nodes.forEach(node => {
      const sx = w / 2 + this.offsetX + this.zoom * node.x;
      const sy = h / 2 + this.offsetY + this.zoom * node.y;

      const scale    = this.options.getNodeScale(node) ?? 1;
      const iconSize = iconWorldSize * this.zoom * scale; // desired visual size

      const type   = this.options.getNodeSpriteType(node);
      const record = this._getSprite(type);

      if (!record || !record.loaded) {
        // placeholder circle
        this.ctx.fillStyle = '#888';
        this.ctx.beginPath();
        this.ctx.arc(sx, sy, iconSize * 0.3, 0, 2 * Math.PI);
        this.ctx.fill();
        return;
      }

      // *** key bit to avoid “jumping”: quantize only the SOURCE size ***
      const sizeKey = quantizeIconSizePx(iconSize);

      // pick the closest already-rasterized bitmap
      let spriteEntry = this._getClosestBitmap(record, sizeKey);

      if (!spriteEntry) {
        // schedule rasterization and draw placeholder for now
        this._scheduleSpriteRaster(type, sizeKey);

        this.ctx.fillStyle = '#888';
        this.ctx.beginPath();
        this.ctx.arc(sx, sy, iconSize * 0.3, 0, 2 * Math.PI);
        this.ctx.fill();
        return;
      }

      const { canvas: bitmap, size: actualSize } = spriteEntry;

      // even if we had a nearby size, make sure we’re precomputing this one too
      if (!record.bitmapMap.has(sizeKey)) {
        this._scheduleSpriteRaster(type, sizeKey);
      }

      const halfDraw = iconSize / 2;

      // IMPORTANT:
      //   - source: always [0..sizeKey]
      //   - dest:   scaled to *smooth* iconSize (no size jumps, only texel resolution jumps)
      this.ctx.drawImage(
        bitmap,
        0, 0, actualSize, actualSize,
        sx - halfDraw, sy - halfDraw,
        iconSize, iconSize
      );
    });

    // --- debug overlay / onAfterDraw stay as you had them ---
    if (this.options.debug && this.debugEl) {
      this.debugEl.textContent =
        `zoom=${this.zoom.toFixed(1)}  ` +
        `offsetX=${this.offsetX.toFixed(1)}  ` +
        `offsetY=${this.offsetY.toFixed(1)}  ` +
        `scrollY=${window.scrollY.toFixed(1)}`;
    }

    if (typeof this.options.onAfterDraw === 'function') {
      this.options.onAfterDraw(this);
    }
  }

  // ---------- events ----------

  _installEvents() {
    this.isDragging  = false;
    this.hoveredNode = null;
    this.dragMoved   = false;   // NEW: tracks whether we actually moved while dragging

    const stopDragging = () => {
      if (!this.isDragging) return;
      this.isDragging = false;
      this.canvas.classList.remove('graph-dragging');
      // NOTE: we **do not** reset dragMoved here – click handler cares about it
    };

    // Start panning on left-button mousedown on the canvas
    this.canvas.addEventListener('mousedown', (e) => {
      if (e.button !== 0) return; // only left mouse button

      this.isDragging = true;
      this.dragMoved  = false;           // NEW: reset movement flag
      this.lastX = e.clientX;
      this.lastY = e.clientY;
      this.canvas.classList.add('graph-dragging');
    });

    // Stop panning when mouse is released or leaves the canvas
    this.canvas.addEventListener('mouseup', stopDragging);
    this.canvas.addEventListener('mouseleave', stopDragging);

    // Mouse move: hover + optional panning (only while over canvas)
    this.canvas.addEventListener('mousemove', (e) => {
      const rect = this.canvas.getBoundingClientRect();
      const mx = e.clientX - rect.left;
      const my = e.clientY - rect.top;
    
      const worldX = (mx - this.logicalWidth  / 2 - this.offsetX) / this.zoom;
      const worldY = (my - this.logicalHeight / 2 - this.offsetY) / this.zoom;
    
      // --- NODE HOVER ---
      const hitNode = this._hitTestNode(mx, my);
    
      if (hitNode !== this.hoveredNode) {
        if (this.hoveredNode && this.options.onNodeHoverEnd) {
          this.options.onNodeHoverEnd();
        }
        this.hoveredNode = hitNode;
      }
    
      if (hitNode && this.options.onNodeHover) {
        this.options.onNodeHover(hitNode, e.clientX, e.clientY, e);
      }
    
      // --- EDGE HOVER (only when not over a node) ---
      if (!hitNode) {
        const hitEdge = this._hitTestEdge(mx, my);
    
        if (hitEdge !== this.hoveredEdge) {
          if (this.hoveredEdge && this.options.onEdgeHoverEnd) {
            this.options.onEdgeHoverEnd();
          }
          this.hoveredEdge = hitEdge;
        }
    
        if (hitEdge && this.options.onEdgeHover) {
          this.options.onEdgeHover(hitEdge, e.clientX, e.clientY, e);
        }
    
        if (!hitEdge && this.hoveredEdge && this.options.onEdgeHoverEnd) {
          this.options.onEdgeHoverEnd();
          this.hoveredEdge = null;
        }
      } else {
        if (this.hoveredEdge && this.options.onEdgeHoverEnd) {
          this.options.onEdgeHoverEnd();
        }
        this.hoveredEdge = null;
      }
    
      // --- panning only when dragging ---
      if (!this.isDragging) return;

      const dx = e.clientX - this.lastX;
      const dy = e.clientY - this.lastY;
      this.lastX = e.clientX;
      this.lastY = e.clientY;

      const MOVE_THRESHOLD = 3; // px; tweak as you like

      if (!this.dragMoved && (Math.abs(dx) > MOVE_THRESHOLD || Math.abs(dy) > MOVE_THRESHOLD)) {
        this.dragMoved = true;

        // if we started dragging while NOT over a node,
        // clear the selection bubble immediately
        if (!this.hoveredNode && this.options.onNodeClick) {
          this.options.onNodeClick(null, e);  // attachSelectionBubble will hide the bubble
        }
      }

      this.offsetX += dx;
      this.offsetY += dy;

      this.draw();
    });

    // Click: now only opens bubble if we *didn't* drag
    this.canvas.addEventListener('click', (e) => {
      const wasDrag = this.dragMoved;

      this.isDragging = false;
      this.dragMoved  = false;
      this.canvas.classList.remove('graph-dragging');

      // If the mouse actually moved, treat it as a pan, not a click
      if (wasDrag) {
        // Optionally: clear selection on drag-end click
        if (this.options.onNodeClick) {
          this.options.onNodeClick(null, e);  // or just `return;` if you don't want that
        }
        return;
      }

      // True "click" → toggle bubble
      if (this.options.onNodeClick) {
        this.options.onNodeClick(this.hoveredNode, e);
      }
    });

    // Wheel zoom (canvas-local)
    this.canvas.addEventListener('wheel', (e) => {
      e.preventDefault();

      const rect = this.canvas.getBoundingClientRect();
      const mx = e.clientX - rect.left;
      const my = e.clientY - rect.top;

      const oldZoom = this.zoom;

      const worldX = (mx - this.logicalWidth  / 2 - this.offsetX) / oldZoom;
      const worldY = (my - this.logicalHeight / 2 - this.offsetY) / oldZoom;

      const speed = 0.0015;
      let factor = Math.exp(-e.deltaY * speed);
      let newZoom = oldZoom * factor;

      newZoom = Math.min(this.maxZoom, Math.max(this.minZoom, newZoom));
      factor  = newZoom / oldZoom;
      this.zoom = newZoom;

      this.offsetX = mx - this.logicalWidth  / 2 - this.zoom * worldX;
      this.offsetY = my - this.logicalHeight / 2 - this.zoom * worldY;

      this.draw();
    }, { passive: false });
  }
}

// -------------------- helpers: hover tooltip & selection bubble --------------------

/**
 * Wire a simple hover tooltip to a viewport.
 *
 * @param {GraphViewport} viewport
 * @param {HTMLElement} rootEl   - container used for positioning (e.g. dupRoot)
 * @param {Object} opts
 *   className      - CSS class for tooltip element
 *   renderTooltip  - (node, tooltipEl) => void  // fill innerHTML/text
 */
export function attachHoverTooltip(viewport, rootEl, opts = {}) {
  const {
    className = 'graph-hover-tooltip',
    renderTooltip = () => {},
  } = opts;

  const tooltip = document.createElement('div');
  tooltip.className = className;
  tooltip.style.position = 'absolute';
  tooltip.style.display  = 'none';
  tooltip.style.pointerEvents = 'none';
  rootEl.appendChild(tooltip);

  const containerRect = () => rootEl.getBoundingClientRect();

  const prevHover     = viewport.options.onNodeHover;
  const prevHoverEnd  = viewport.options.onNodeHoverEnd;

  viewport.options.onNodeHover = (node, clientX, clientY, event) => {
    if (prevHover) prevHover(node, clientX, clientY, event);

    renderTooltip(node, tooltip);

    const rect = containerRect();
    tooltip.style.left   = `${clientX - rect.left + 10}px`;
    tooltip.style.top    = `${clientY - rect.top  + 10}px`;
    tooltip.style.display = 'block';
  };

  viewport.options.onNodeHoverEnd = () => {
    if (prevHoverEnd) prevHoverEnd();
    tooltip.style.display = 'none';
  };
}

export function attachEdgeHoverTooltip(viewport, rootEl, opts = {}) {
  const {
    className = 'graph-edge-tooltip',
    renderTooltip = () => {},
  } = opts;

  const tooltip = document.createElement('div');
  tooltip.className = className;
  tooltip.style.position = 'absolute';
  tooltip.style.display  = 'none';
  tooltip.style.pointerEvents = 'none';
  rootEl.appendChild(tooltip);

  const containerRect = () => rootEl.getBoundingClientRect();

  const prevHover    = viewport.options.onEdgeHover;
  const prevHoverEnd = viewport.options.onEdgeHoverEnd;

  viewport.options.onEdgeHover = (edge, clientX, clientY, event) => {
    if (prevHover) prevHover(edge, clientX, clientY, event);

    renderTooltip(edge, tooltip);

    const rect = containerRect();
    tooltip.style.left   = `${clientX - rect.left + 10}px`;
    tooltip.style.top    = `${clientY - rect.top  + 10}px`;
    tooltip.style.display = 'block';
  };

  viewport.options.onEdgeHoverEnd = () => {
    if (prevHoverEnd) prevHoverEnd();
    tooltip.style.display = 'none';
  };
}

/**
 * Wire a persistent "selected node" bubble to a viewport.
 *
 * Handles:
 *   - click on node = show / toggle bubble
 *   - click on empty canvas = hide bubble
 *   - re-position on pan/zoom (via onAfterDraw)
 *
 * @param {GraphViewport} viewport
 * @param {HTMLElement} rootEl
 * @param {Object} opts
 *   className     - CSS class for bubble element
 *   renderBubble  - (node, bubbleEl) => void   // fill contents
 *   radiusFactor  - multiplier for sprite radius to offset bubble vertically
 */
export function attachSelectionBubble(viewport, rootEl, opts = {}) {
  const {
    className    = 'graph-selected-bubble',
    renderBubble = () => {},
    radiusFactor = 0.5,
  } = opts;

  const bubble = document.createElement('div');
  bubble.className = className;
  bubble.style.position = 'absolute';
  bubble.style.display  = 'none';
  rootEl.appendChild(bubble);

  let selectedNode = null;

  function updatePosition() {
    if (!selectedNode) return;

    const bubbleW = bubble.offsetWidth;
    const bubbleH = bubble.offsetHeight;
    if (!bubbleW || !bubbleH) return;

    // Node pos in canvas coords
    const { x, y } = viewport.worldToScreen(selectedNode.x, selectedNode.y);

    // Convert to rootEl-local coords
    const containerRect = rootEl.getBoundingClientRect();
    const canvasRect    = viewport.canvas.getBoundingClientRect();

    const sx = canvasRect.left + x - containerRect.left;
    const sy = canvasRect.top  + y - containerRect.top;

    const containerW = containerRect.width;
    const containerH = containerRect.height;

    const scale = viewport.options.getNodeScale
      ? (viewport.options.getNodeScale(selectedNode) ?? 1)
      : 1;

    const radiusPx =
      viewport.options.iconWorldSize * viewport.zoom * scale * radiusFactor;

    let left = sx - bubbleW / 2;
    let top  = sy + radiusPx;

    // clamp into visible area
    left = Math.min(Math.max(left, 0), containerW - bubbleW);
    top  = Math.min(Math.max(top,  0), containerH - bubbleH);

    bubble.style.left = `${left}px`;
    bubble.style.top  = `${top}px`;
  }

  // chain into existing callbacks if any
  const prevClick = viewport.options.onNodeClick;
  const prevAfter = viewport.options.onAfterDraw;

  viewport.options.onNodeClick = (node, event) => {
    // keep original behavior
    if (prevClick) prevClick(node, event);

    // click away → hide
    if (!node) {
      selectedNode = null;
      bubble.style.display = 'none';
      return;
    }

    // toggle off when clicking same node
    if (selectedNode && selectedNode.id === node.id && bubble.style.display !== 'none') {
      selectedNode = null;
      bubble.style.display = 'none';
      return;
    }

    // select + rebuild
    selectedNode = node;
    bubble.innerHTML = '';
    renderBubble(node, bubble);
    bubble.style.display = 'block';
    updatePosition();
  };

  viewport.options.onAfterDraw = (vp) => {
    if (prevAfter) prevAfter(vp);
    updatePosition();
  };
}