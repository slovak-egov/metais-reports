import {
  getNodeMass,
  getSpringParams,
  getRepulsionParams,
  CENTER_FORCE,
  DAMPING,
  REPULSION_DEFAULTS,
} from './physicsParams.js';

export class PhysicsSystem {
  constructor(options = {}) {
    this.nodes = [];
    this.edges = [];

    this.nodeState = new Map(); // id → { vx, vy, mass }

    this.timeScale = options.timeScale ?? 1.0;   // global speed multiplier
    this.maxDt     = options.maxDt ?? 0.03;      // clamp big frame jumps

    // helper predicates
    this.isSpringEdge = options.isSpringEdge || ((edge) => edge.kind === 'relation');
    this.isRepelActive = options.isRepelActive ||
      ((n) => true); // can later disable for some node types
  }

  /**
   * Attach to a graph scene. Nodes are mutated in-place (x,y).
   * Call this whenever you rebuild the scene.
   */
  setGraph(nodes, edges) {
    this.nodes = nodes || [];
    this.edges = edges || [];

    // rebuild state with preserved velocities where possible
    const oldState = this.nodeState;
    this.nodeState = new Map();

    for (const n of this.nodes) {
      const prev = oldState.get(n.id);
      const mass = getNodeMass(n);

      if (prev) {
        this.nodeState.set(n.id, {
          vx: prev.vx,
          vy: prev.vy,
          mass,
        });
      } else {
        this.nodeState.set(n.id, {
          vx: 0,
          vy: 0,
          mass,
        });
      }
    }
  }

  /**
   * Advance physics by dt seconds.
   */
  step(dtRaw) {
    if (!this.nodes.length) return;

    let dt = dtRaw * this.timeScale;
    if (!Number.isFinite(dt) || dt <= 0) return;
    if (dt > this.maxDt) dt = this.maxDt;

    const n = this.nodes.length;

    // Temporary force buffers
    const fx = new Array(n).fill(0);
    const fy = new Array(n).fill(0);

    // Index lookup for nodes
    const indexById = new Map();
    this.nodes.forEach((node, i) => indexById.set(node.id, i));

    // ---- 1) Spring forces along edges (relations) ----
    for (const edge of this.edges) {
      if (!this.isSpringEdge(edge)) continue;

      const i = indexById.get(edge.source);
      const j = indexById.get(edge.target);
      if (i == null || j == null) continue;

      const ni = this.nodes[i];
      const nj = this.nodes[j];

      const dx = nj.x - ni.x;
      const dy = nj.y - ni.y;
      const dist = Math.hypot(dx, dy) || 1e-6;

      const { k, restLength } = getSpringParams(edge, ni, nj);

      // Hooke-like: F = k * (dist - restLength)
      const stretch = dist - restLength;
      if (stretch === 0) continue;

      const Fmag = k * stretch;
      const ux = dx / dist;
      const uy = dy / dist;

      const Fx = Fmag * ux;
      const Fy = Fmag * uy;

      fx[i] += Fx;
      fy[i] += Fy;
      fx[j] -= Fx;
      fy[j] -= Fy;
    }

    // ---- 2) Repulsion (overlap avoidance) ----
    if (REPULSION_DEFAULTS.enabled) {
      for (let i = 0; i < n; i++) {
        const ni = this.nodes[i];
        if (!this.isRepelActive(ni)) continue;

        for (let j = i + 1; j < n; j++) {
          const nj = this.nodes[j];
          if (!this.isRepelActive(nj)) continue;

          const dx = nj.x - ni.x;
          const dy = nj.y - ni.y;
          const dist = Math.hypot(dx, dy) || 1e-6;

          const { k, repelDistance } = getRepulsionParams(ni, nj);
          if (dist >= repelDistance) continue;

          const overlap = repelDistance - dist;
          if (overlap <= 0) continue;

          const ux = dx / dist;
          const uy = dy / dist;

          // push them apart
          const Fmag = k * overlap;

          const Fx = Fmag * ux;
          const Fy = Fmag * uy;

          fx[i] -= Fx;
          fy[i] -= Fy;
          fx[j] += Fx;
          fy[j] += Fy;
        }
      }
    }

    // ---- 3) Global center pull ----
    if (CENTER_FORCE.enabled && CENTER_FORCE.k > 0) {
      const kC = CENTER_FORCE.k;

      for (let i = 0; i < n; i++) {
        const ni = this.nodes[i];
        const x = ni.x;
        const y = ni.y;

        // F_center = -k * r
        fx[i] += -kC * x;
        fy[i] += -kC * y;
      }
    }

    // ---- 4) Damping (per node) ----
    const gamma = DAMPING.gamma;

    // ---- 5) Integrate velocities & positions ----
    for (let i = 0; i < n; i++) {
      const ni = this.nodes[i];
      const state = this.nodeState.get(ni.id);
      if (!state) continue;

      const { mass } = state;
      const invMass = mass > 0 ? 1 / mass : 0;

      // damping: F_damp = -gamma * v
      const Fvx = fx[i] - gamma * state.vx;
      const Fvy = fy[i] - gamma * state.vy;

      const ax = Fvx * invMass;
      const ay = Fvy * invMass;

      state.vx += ax * dt;
      state.vy += ay * dt;

      ni.x += state.vx * dt;
      ni.y += state.vy * dt;
    }
  }
}