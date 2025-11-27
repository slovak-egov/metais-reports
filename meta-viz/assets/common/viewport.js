function rasterizeSVGToCanvas(img, size) {
  const off = document.createElement('canvas');
  off.width = size;
  off.height = size;
  const ctx = off.getContext('2d');
  ctx.drawImage(img, 0, 0, size, size);
  return off;
}

function drawArrow(ctx, x1, y1, x2, y2, headSize) {
  // main line
  ctx.beginPath();
  ctx.moveTo(x1, y1);
  ctx.lineTo(x2, y2);
  ctx.stroke();

  const dx = x2 - x1;
  const dy = y2 - y1;
  const len = Math.hypot(dx, dy) || 1;
  const ux = dx / len;
  const uy = dy / len;

  const hx = x2 - ux * headSize;
  const hy = y2 - uy * headSize;

  ctx.beginPath();
  ctx.moveTo(x2, y2);
  ctx.lineTo(
    hx + (-uy) * headSize * 0.6,
    hy + (ux)  * headSize * 0.6
  );
  ctx.lineTo(
    hx - (-uy) * headSize * 0.6,
    hy - (ux)  * headSize * 0.6
  );
  ctx.closePath();
  ctx.fill();
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
      showGrid: true,
      gridMinorColor: '#3b2c20',
      gridMajorColor: '#5c4a32',
      axesColor:      '#888',
      initialZoom:    20,
      minZoom:        4,
      maxZoom:        1000,
      debug:          false,
      canvasClass:    'graph-canvas',
      debugClass:     'graph-debug',
      // how big should a sprite be in world units
      iconWorldSize:  1.0,

      // styling callbacks
      getNodeSpriteType: node => node.type || 'UNKNOWN',
      getEdgeStyle: edge => ({
        color: edge.kind === 'relation' ? '#e4b84a' : '#444',
        width: edge.kind === 'relation' ? 1.5 : 1,
        arrow: edge.kind === 'relation',
      }),

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

    this.isDragging = false;
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
    record = { svgImg, bitmapMap, loaded: false };
    this.spriteCache.set(type, record);

    svgImg.onload = () => {
      record.loaded = true;
      this.draw();
    };
    svgImg.onerror = () => {
      console.warn(`Failed to load sprite ${type}, trying fallback.`);
      if (!svgImg.src.endsWith('default_application.svg')) {
        svgImg.src = 'sprites/default_application.svg';
      }
    };

    svgImg.src = `sprites/${type}.svg`;
    return record;
  }

  // ---------- layout / drawing ----------

  resize() {
    const dpr = window.devicePixelRatio || 1;
    this.devicePixelRatioUsed = dpr;

    const w = this.canvas.clientWidth;
    const h = this.canvas.clientHeight;
    if (!w || !h) return;

    this.logicalWidth  = w;
    this.logicalHeight = h;

    this.canvas.width  = w * dpr;
    this.canvas.height = h * dpr;

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
      const stepWorld = 1;
      const stepPx = this.zoom * stepWorld;

      if (stepPx >= 4) {
        const worldXMin = (0   - w / 2 - this.offsetX) / this.zoom;
        const worldXMax = (w   - w / 2 - this.offsetX) / this.zoom;
        const worldYMin = (0   - h / 2 - this.offsetY) / this.zoom;
        const worldYMax = (h   - h / 2 - this.offsetY) / this.zoom;

        const xStart = Math.floor(worldXMin);
        const xEnd   = Math.ceil(worldXMax);
        const yStart = Math.floor(worldYMin);
        const yEnd   = Math.ceil(worldYMax);

        for (let ix = xStart; ix <= xEnd; ix++) {
          const xScreen = w / 2 + this.offsetX + this.zoom * ix;
          const major = (ix % 10 === 0);

          g.strokeStyle = major ? this.options.gridMajorColor : this.options.gridMinorColor;
          g.lineWidth   = major ? 2 / this.devicePixelRatioUsed : 1 / this.devicePixelRatioUsed;

          g.beginPath();
          g.moveTo(xScreen, 0);
          g.lineTo(xScreen, h);
          g.stroke();
        }

        for (let iy = yStart; iy <= yEnd; iy++) {
          const yScreen = h / 2 + this.offsetY + this.zoom * iy;
          const major = (iy % 10 === 0);

          g.strokeStyle = major ? this.options.gridMajorColor : this.options.gridMinorColor;
          g.lineWidth   = major ? 2 / this.devicePixelRatioUsed : 1 / this.devicePixelRatioUsed;

          g.beginPath();
          g.moveTo(0, yScreen);
          g.lineTo(w, yScreen);
          g.stroke();
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

    // --- edges ---
    const nodes = this.scene.nodes || [];
    const edges = this.scene.edges || [];

    const iconWorldSize = this.options.iconWorldSize;

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
      const width = style.width || 1;
      const arrow = !!style.arrow;

      const startCut = iconWorldSize * this.zoom * 0.6;
      const endCut   = iconWorldSize * this.zoom * 0.9;

      const clipped = shortenLine(ax, ay, bx, by, startCut, endCut);

      g.strokeStyle = color;
      g.fillStyle   = color;
      g.lineWidth   = width / this.devicePixelRatioUsed;

      if (arrow) {
        const arrowSize = Math.min(iconWorldSize * this.zoom * 0.8, 20);
        drawArrow(g, clipped.sx, clipped.sy, clipped.ex, clipped.ey, arrowSize);
      } else {
        g.beginPath();
        g.moveTo(clipped.sx, clipped.sy);
        g.lineTo(clipped.ex, clipped.ey);
        g.stroke();
      }
    });

    // --- nodes (sprites) ---
    nodes.forEach(node => {
      const sx = w / 2 + this.offsetX + this.zoom * node.x;
      const sy = h / 2 + this.offsetY + this.zoom * node.y;

      const iconSize = iconWorldSize * this.zoom;
      const sizeKey = Math.max(4, Math.round(iconSize));

      const type = this.options.getNodeSpriteType(node);
      const record = this._getSprite(type);

      if (!record || !record.loaded) {
        // fallback circle
        this.ctx.fillStyle = '#888';
        this.ctx.beginPath();
        this.ctx.arc(sx, sy, iconSize * 0.3, 0, 2 * Math.PI);
        this.ctx.fill();
        return;
      }

      let bitmap = record.bitmapMap.get(sizeKey);
      if (!bitmap) {
        bitmap = rasterizeSVGToCanvas(record.svgImg, sizeKey);
        record.bitmapMap.set(sizeKey, bitmap);
      }

      const half = sizeKey / 2;
      this.ctx.drawImage(bitmap, sx - half, sy - half, sizeKey, sizeKey);
    });

    // --- debug overlay ---
    if (this.options.debug && this.debugEl) {
      this.debugEl.textContent =
        `zoom=${this.zoom.toFixed(1)}  ` +
        `offsetX=${this.offsetX.toFixed(1)}  offsetY=${this.offsetY.toFixed(1)}`;
    }
  }

  // ---------- events ----------

  _installEvents() {
    this.canvas.addEventListener('mousedown', (e) => {
      this.isDragging = true;
      this.lastX = e.clientX;
      this.lastY = e.clientY;
      this.canvas.classList.add('graph-dragging');
    });

    window.addEventListener('mouseup', () => {
      if (!this.isDragging) return;
      this.isDragging = false;
      this.canvas.classList.remove('graph-dragging');
    });

    window.addEventListener('mousemove', (e) => {
      const rect = this.canvas.getBoundingClientRect();
      const mx = e.clientX - rect.left;
      const my = e.clientY - rect.top;

      const worldX = (mx - this.logicalWidth  / 2 - this.offsetX) / this.zoom;
      const worldY = (my - this.logicalHeight / 2 - this.offsetY) / this.zoom;

      if (this.options.debug && this.debugEl) {
        this.debugEl.textContent =
          `mx=${mx.toFixed(1)}, my=${my.toFixed(1)}  ` +
          `worldX=${worldX.toFixed(2)}, worldY=${worldY.toFixed(2)}  ` +
          `zoom=${this.zoom.toFixed(1)}`;
      }

      if (!this.isDragging) return;

      const dx = e.clientX - this.lastX;
      const dy = e.clientY - this.lastY;
      this.lastX = e.clientX;
      this.lastY = e.clientY;

      this.offsetX += dx;
      this.offsetY += dy;

      this.draw();
    });

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