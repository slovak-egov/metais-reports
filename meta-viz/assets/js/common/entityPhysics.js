import {
  getNodeMass,
  getSpringParams,
  getRepulsionParams,
  CENTER_FORCE,
  DAMPING,
  REPULSION_DEFAULTS,
  SPRING_DAMPING,
  FRICTION,
} from './physicsParams.js';

/**
 * Apply dry (Coulomb) friction to a single node.
 *
 * Fx,Fy  - current net force *without* friction
 * vx,vy  - current velocity
 * mass   - node mass
 * muStatic, muKinetic - coefficients of static / kinetic friction
 *
 * Returns:
 *   {
 *     Fx, Fy,   // friction-adjusted force
 *     Fmag,     // |F|
 *     stick,    // true if static friction locks it (velocity should be zeroed)
 *   }
 */
function applyFriction(Fx, Fy, vx, vy, mass, muStatic, muKinetic) {
  let Fvx = Fx;
  let Fvy = Fy;

  const Fmag = Math.hypot(Fvx, Fvy);
  const vmag = Math.hypot(vx,  vy);

  // Small velocity threshold to decide "at rest"
  const V_EPS = 1e-4;

  // --- STATIC FRICTION: only if we're basically not moving ---
  // If v ≈ 0 and net force is smaller than μ_s * m, we stick:
  // friction cancels *all* net force and velocity must be kept at 0.
  if (vmag < V_EPS && Fmag < muStatic * mass) {
    return {
      Fx: 0,
      Fy: 0,
      Fmag: 0,
      stick: true,
    };
  }

  // --- KINETIC FRICTION: oppose motion with magnitude μ_k * m ---
  if (vmag > 0 && muKinetic > 0) {
    const F_fric_mag = muKinetic * mass; // g ≈ 1 in our "world"
    const ux = vx / vmag;
    const uy = vy / vmag;

    // friction force opposite to velocity
    Fvx -= F_fric_mag * ux;
    Fvy -= F_fric_mag * uy;
  }

  const newMag = Math.hypot(Fvx, Fvy);
  return {
    Fx: Fvx,
    Fy: Fvy,
    Fmag: newMag,
    stick: false,
  };
}

export class PhysicsSystem {
  constructor(options = {}) {
    this.nodes = [];
    this.edges = [];

    this.nodeState = new Map(); // id → { vx, vy, mass }

    this.timeScale = options.timeScale ?? 3.0;  // global speed multiplier
    this.maxDt     = options.maxDt     ?? 1.0;  // clamp big frame jumps

    // helper predicates
    this.isSpringEdge = options.isSpringEdge || ((edge) => edge.kind === 'relation');
    this.isRepelActive = options.isRepelActive ||
      ((n) => true); // can later disable for some node types
  }

  setGraph(nodes, edges) {
    this.nodes = nodes || [];
    this.edges = edges || [];

    const oldState = this.nodeState;
    this.nodeState = new Map();

    // ---- compute degree per node id ----
    const degree = new Map();
    for (const edge of this.edges) {
      if (!this.isSpringEdge(edge)) continue;

      if (edge.source != null) {
        degree.set(edge.source, (degree.get(edge.source) || 0) + 1);
      }
      if (edge.target != null) {
        degree.set(edge.target, (degree.get(edge.target) || 0) + 1);
      }
    }

    for (const n of this.nodes) {
      const prev = oldState.get(n.id);
      const deg  = degree.get(n.id) || 0;

      const mass = getNodeMass(n, deg);

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
    if (!this.nodes.length) return 0;

    // --- base dt from wall-clock + global scale ---
    let dt = dtRaw * this.timeScale;
    if (!Number.isFinite(dt) || dt <= 0) return 0;
    if (dt > this.maxDt) dt = this.maxDt;

    const n  = this.nodes.length;
    const fx = new Array(n).fill(0);
    const fy = new Array(n).fill(0);

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
      const stretch = dist - restLength;
      if (stretch === 0) continue;

      const ux = dx / dist;
      const uy = dy / dist;

      const FmagSpring = k * stretch;

      let Fx = FmagSpring * ux;
      let Fy = FmagSpring * uy;

      // spring-local damping along edge direction
      const gammaSpring = SPRING_DAMPING.gamma || 0.0;
      if (gammaSpring > 0) {
        const stateI = this.nodeState.get(ni.id);
        const stateJ = this.nodeState.get(nj.id);

        if (stateI && stateJ) {
          const relVx = stateJ.vx - stateI.vx;
          const relVy = stateJ.vy - stateI.vy;

          const vRelAlong = relVx * ux + relVy * uy;
          const Fdamp = gammaSpring * vRelAlong;

          Fx += Fdamp * ux;
          Fy += Fdamp * uy;
        }
      }

      fx[i] += Fx;
      fy[i] += Fy;
      fx[j] -= Fx;
      fy[j] -= Fy;
    }

    // ---- 2) Repulsion ----
    if (REPULSION_DEFAULTS.enabled) {
      for (let i = 0; i < n; i++) {
        const ni = this.nodes[i];
        if (!this.isRepelActive(ni)) continue;

        for (let j = i + 1; j < n; j++) {
          const nj = this.nodes[j];
          if (!this.isRepelActive(nj)) continue;

          const dx = nj.x - ni.x;
          const dy = nj.y - ni.y;
          let dist = Math.hypot(dx, dy);

          let ux, uy;
          if (dist < 1e-6) {
            const angle = Math.random() * 2 * Math.PI;
            ux = Math.cos(angle);
            uy = Math.sin(angle);
            dist = 1e-6;
          } else {
            ux = dx / dist;
            uy = dy / dist;
          }

          const { k, repelDistance } = getRepulsionParams(ni, nj);
          if (dist >= repelDistance) continue;

          const overlap = repelDistance - dist;
          if (overlap <= 0) continue;

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

    // ---- 3) Center pull ----
    if (CENTER_FORCE.enabled && CENTER_FORCE.k > 0) {
      const kC = CENTER_FORCE.k;
      for (let i = 0; i < n; i++) {
        const ni = this.nodes[i];
        fx[i] += -kC * ni.x;
        fy[i] += -kC * ni.y;
      }
    }

    const gammaGlobal = DAMPING.gamma || 0.0;

    // friction params (allow separate μ_s and μ_k, but fall back if needed)
    const muStatic  = FRICTION.muS ?? FRICTION.muK ?? 0.0;
    const muKinetic = FRICTION.muK ?? muStatic;

    const netFx = new Array(n).fill(0);
    const netFy = new Array(n).fill(0);
    const stick = new Array(n).fill(false);

    // ---- 4) Apply damping + friction, then compute vmax/amax for CFL ----
    let vmax = 0;
    let amax = 0;

    for (let i = 0; i < n; i++) {
      const ni    = this.nodes[i];
      const state = this.nodeState.get(ni.id);
      if (!state) continue;

      const { mass } = state;
      const invMass = mass > 0 ? 1 / mass : 0;

      // raw + global linear damping
      const Fvx = fx[i] - gammaGlobal * state.vx;
      const Fvy = fy[i] - gammaGlobal * state.vy;

      // friction acts in world frame
      const fr = applyFriction(
        Fvx, Fvy,
        state.vx, state.vy,
        mass,
        muStatic,
        muKinetic
      );

      netFx[i] = fr.Fx;
      netFy[i] = fr.Fy;
      stick[i] = fr.stick;

      const vmag = Math.hypot(state.vx, state.vy);
      const amag = fr.Fmag * invMass;

      if (vmag > vmax) vmax = vmag;
      if (amag > amax) amax = amag;
    }

    // ---- CFL limits on dt ----
    const L = 1.0;

    // Velocity CFL: node shouldn’t travel more than ~CFL_VEL * L per step
    const CFL_VEL = 0.3;
    if (vmax > 0) {
      const dtVel = CFL_VEL * L / vmax;
      dt = Math.min(dt, dtVel);
    }

    // Acceleration-based: 0.5 a dt² <= CFL_ACC * L
    const CFL_ACC = 0.001;
    if (amax > 0) {
      const dtAcc = Math.sqrt((2 * CFL_ACC * L) / amax);
      dt = Math.min(dt, dtAcc);
    }

    const MIN_DT = 1e-8;
    if (dt < MIN_DT) dt = MIN_DT;

    // ---- 5) Integrate using the SAME netFx/netFy ----
    for (let i = 0; i < n; i++) {
      const ni    = this.nodes[i];
      const state = this.nodeState.get(ni.id);
      if (!state) continue;

      const { mass } = state;
      const invMass = mass > 0 ? 1 / mass : 0;

      if (stick[i]) {
        // Static friction: fully locked → no motion
        state.vx = 0;
        state.vy = 0;
        continue;
      }

      const Fx = netFx[i];
      const Fy = netFy[i];

      const ax = Fx * invMass;
      const ay = Fy * invMass;

      state.vx += ax * dt;
      state.vy += ay * dt;

      ni.x += state.vx * dt;
      ni.y += state.vy * dt;
    }

    this.lastDt = dt;
    return dt;
  }
}