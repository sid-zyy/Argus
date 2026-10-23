// ARGUS: the agent team as one presence. A rotating node-and-edge sphere is the mind (orchestrator);
// each agent owns a region of it and a glass card beside it. When an agent works, its region lights up,
// sparks run along its edges, the sphere turns that region toward its card, and pulses travel the link.
// Drag to rotate; hover a region to see whose it is; click a region or a card to see that agent's steps.
const Argus = (() => {
  const AGENTS = {
    triage:       { name: "Investigator", role: "Reads the logs",          dark: [94, 206, 255],  light: [14, 132, 196],  icon: "eye",    x: .15, y: .24, dir: [-.7, -.5, .5] },
    code_history: { name: "Change finder", role: "Checks recent changes",  dark: [255, 128, 210], light: [200, 50, 140],  icon: "branch", x: .11, y: .52, dir: [-.95, .05, -.3] },
    verifier:     { name: "Fact checker", role: "Tries to prove it wrong", dark: [160, 132, 255], light: [104, 76, 214],  icon: "scale",  x: .15, y: .80, dir: [-.6, .65, .45] },
    fix:          { name: "Fixer",        role: "Suggests a fix",          dark: [255, 186, 108], light: [204, 110, 8],   icon: "spark",  x: .85, y: .24, dir: [.7, -.5, .5] },
    sandbox:      { name: "Test lab",     role: "Tests the fix safely",    dark: [120, 168, 255], light: [44, 100, 214],  icon: "cube",   x: .89, y: .52, dir: [.95, .05, -.3] },
    reporter:     { name: "Writer",       role: "Writes the summary",      dark: [100, 232, 206], light: [10, 140, 124],  icon: "doc",    x: .85, y: .80, dir: [.6, .65, .45] },
    human:        { name: "You",          role: "Approve the fix",         dark: [255, 214, 150], light: [190, 128, 16],  icon: "person", x: .50, y: .92, dir: [0, .95, -.3] },
  };
  const ACTOR = { triage: "triage", code_history: "code_history", verifier: "verifier", fix: "fix",
    sandbox: "sandbox", reporter: "reporter", human: "human" };
  const PALETTES = {
    dark: { BASE: [150, 165, 255], GOOD: [86, 232, 160], BAD: [255, 96, 116], GOLD: [255, 214, 150], CALM: [96, 226, 196], WHITE: [255, 255, 255] },
    light: { BASE: [92, 98, 150], GOOD: [24, 140, 84], BAD: [204, 54, 48], GOLD: [190, 128, 16], CALM: [10, 140, 124], WHITE: [80, 72, 216] },
  };
  const theme = () => (document.documentElement.dataset.theme === "dark" ? "dark" : "light");
  const P = (k) => PALETTES[theme()][k];
  const col = (key) => AGENTS[key][theme()];
  // plain-language description of a tool call, for captions and cards
  const TOOL_TEXT = {
    search_logs: (a) => `Searching ${a.source || "the logs"} for "${a.pattern}"`,
    read_lines: (a) => `Reading lines ${a.start_line}-${a.end_line} of ${a.source}`,
    list_sources: () => "Checking which logs are available",
    get_metrics: () => "Looking at the service's health metrics",
    git_log: () => "Looking at recent code changes",
    git_show: (a) => `Reading change ${a.sha}`,
    read_file: (a) => `Reading ${a.path}`,
    list_files: () => "Looking through the code",
  };
  const VERDICT_TEXT = { supported: "Confirmed", refuted: "Ruled out", insufficient: "Not enough proof" };

  const ICONS = {
    eye: '<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
    branch: '<circle cx="6" cy="5" r="2"/><circle cx="6" cy="19" r="2"/><circle cx="18" cy="8" r="2"/><path d="M6 7v10M18 10c0 4-6 3-11.5 7"/>',
    scale: '<path d="M12 4v16M6 20h12M5 8h14M5 8l-3 6a3 3 0 0 0 6 0zM19 8l-3 6a3 3 0 0 0 6 0z"/>',
    spark: '<path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5 18 18M6 18l2.5-2.5M15.5 8.5 18 6"/>',
    cube: '<path d="M12 2 21 7v10l-9 5-9-5V7z"/><path d="M3 7l9 5 9-5M12 12v10"/>',
    doc: '<path d="M6 2h8l5 5v15H6z"/><path d="M14 2v5h5M9 13h7M9 17h7"/>',
    person: '<circle cx="12" cy="8" r="4"/><path d="M4 21c1.5-4 4.5-6 8-6s6.5 2 8 6"/>',
  };

  const mix = (a, b, t) => a.map((v, i) => v + (b[i] - v) * t);
  const rgba = (c, a) => `rgba(${c[0] | 0},${c[1] | 0},${c[2] | 0},${a})`;
  const norm = (v) => { const l = Math.hypot(...v); return v.map((x) => x / l); };
  const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
  const smooth = (e0, e1, x) => { const t = Math.min(1, Math.max(0, (x - e0) / (e1 - e0))); return t * t * (3 - 2 * t); };
  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // Geodesic-looking mesh: Fibonacci points joined to their nearest neighbours.
  function mesh(n) {
    const pts = [];
    const ga = Math.PI * (3 - Math.sqrt(5));
    for (let i = 0; i < n; i++) {
      const y = 1 - (i / (n - 1)) * 2, r = Math.sqrt(1 - y * y), th = ga * i;
      pts.push([Math.cos(th) * r, y, Math.sin(th) * r]);
    }
    const edges = new Set(), nbrs = pts.map(() => []);
    pts.forEach((p, i) => {
      pts.map((q, j) => [j, dot(p, q)]).filter(([j]) => j !== i).sort((a, b) => b[1] - a[1]).slice(0, 4).forEach(([j]) => {
        const k = i < j ? `${i}-${j}` : `${j}-${i}`;
        if (!edges.has(k)) { edges.add(k); nbrs[i].push(j); nbrs[j].push(i); }
      });
    });
    return { pts, edges: [...edges].map((k) => k.split("-").map(Number)), nbrs };
  }

  function mount(container, { compact = false } = {}) {
    container.classList.add("argus");
    if (compact) container.classList.add("argus-compact");
    container.innerHTML = `
      <div class="argus-stage">
        <canvas class="argus-canvas" aria-label="Argus: the agent team as a rotating network sphere. Drag to rotate."></canvas>
        <div class="argus-toast" role="status"></div>
        <div class="argus-tip"></div>
        <div class="argus-center">
          <div class="argus-caption"><span class="argus-who">Argus</span><span class="argus-line">All quiet. Argus is keeping an eye on things.</span></div>
        </div>
        <div class="argus-nodes"></div>
        <div class="argus-detail" hidden></div>
      </div>`;
    const $ = (s) => container.querySelector(s);
    const stage = $(".argus-stage"), canvas = $(".argus-canvas"), ctx = canvas.getContext("2d");
    const toast = $(".argus-toast"), tip = $(".argus-tip"), who = $(".argus-who"), line = $(".argus-line");
    const nodesBox = $(".argus-nodes"), detail = $(".argus-detail");
    let onSelect = null;

    const M = mesh(compact ? 150 : 200);
    const anchors = Object.fromEntries(Object.entries(AGENTS).map(([k, a]) => [k, norm(a.dir)]));
    // each mesh point belongs to the nearest agent region
    const owner = M.pts.map((p) => Object.entries(anchors).reduce((best, [k, a]) => (dot(p, a) > best[1] ? [k, dot(p, a)] : best), [null, -2])[0]);
    const anchorNode = Object.fromEntries(Object.entries(anchors).map(([k, a]) => [k, M.pts.reduce((bi, p, i) => (dot(p, a) > dot(M.pts[bi], a) ? i : bi), 0)]));

    const S = {
      w: 0, h: 0, dpr: 1, t: 0, yaw: .4, pitch: -.25, vyaw: 0, vpitch: 0, lastTouch: -1e9,
      tint: P('BASE').slice(), tintTarget: P('BASE'), energy: .3, energyTarget: .3, beat: 0,
      active: null, done: new Set(), mood: "idle", ring: 0, ringTarget: 0, ringColor: P('GOLD'),
      sparks: [], packets: [], ripples: [], proj: [], hoverNode: -1, hoverAgent: null, selected: null, dragging: false, cards: {},
    };

    const nodes = {};
    const history = Object.fromEntries(Object.keys(AGENTS).map((k) => [k, []]));
    if (!compact) {
      for (const [key, a] of Object.entries(AGENTS)) {
        const el = document.createElement("button");
        el.type = "button";
        el.className = "argus-node";
        el.dataset.agent = key;
        el.style.setProperty("--x", `${a.x * 100}%`);
        el.style.setProperty("--y", `${a.y * 100}%`);
        el.style.setProperty("--c", col(key).join(","));
        el.innerHTML = `<div class="an-icon"><svg viewBox="0 0 24 24">${ICONS[a.icon]}</svg></div>
          <div class="an-body"><div class="an-name">${a.name}<span class="an-result"></span></div>
          <div class="an-status">${a.role}</div></div>`;
        el.addEventListener("click", () => select(key));
        el.addEventListener("mouseenter", () => { S.hoverAgent = key; });
        el.addEventListener("mouseleave", () => { S.hoverAgent = null; });
        nodesBox.appendChild(el);
        nodes[key] = { el, status: el.querySelector(".an-status"), result: el.querySelector(".an-result"), role: a.role };
      }
    }

    function applyTheme() {
      Object.entries(nodes).forEach(([k, n]) => n.el.style.setProperty("--c", col(k).join(",")));
      S.tint = S.tintTarget = P("BASE");
      setActive(S.active);
    }
    new MutationObserver(applyTheme).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });

    // ---- geometry ----
    function layout() {
      const c = canvas.getBoundingClientRect();
      S.dpr = Math.min(2, window.devicePixelRatio || 1);
      S.w = c.width; S.h = c.height;
      canvas.width = c.width * S.dpr; canvas.height = c.height * S.dpr;
      S.linked = !compact && getComputedStyle(nodesBox).position === "absolute";
      S.cx = c.width / 2; S.cy = c.height * (S.linked ? .47 : .5);
      S.R = Math.min(c.width, c.height) * (compact ? .36 : S.linked ? .22 : .34);
      S.cards = {};
      if (!S.linked) return;
      for (const [k, n] of Object.entries(nodes)) {
        const b = n.el.getBoundingClientRect();
        if (k === "human") S.cards[k] = [b.left + b.width / 2 - c.left, b.top - c.top];
        else if (b.left + b.width / 2 - c.left < S.cx) S.cards[k] = [b.right - c.left, b.top + b.height / 2 - c.top];
        else S.cards[k] = [b.left - c.left, b.top + b.height / 2 - c.top];
      }
    }
    new ResizeObserver(layout).observe(stage);

    function rotate(p) {
      const cy = Math.cos(S.yaw), sy = Math.sin(S.yaw), cp = Math.cos(S.pitch), sp = Math.sin(S.pitch);
      const x = p[0] * cy + p[2] * sy, z1 = -p[0] * sy + p[2] * cy;
      return [x, p[1] * cp - z1 * sp, p[1] * sp + z1 * cp];
    }
    function project(p) {
      const [x, y, z] = rotate(p);
      const s = 3.2 / (3.2 - z);
      return [S.cx + x * S.R * s, S.cy + y * S.R * s, z, s];
    }
    // yaw/pitch that bring an agent's region to the front, turned a little toward its card
    function facing(key) {
      const a = anchors[key], ag = AGENTS[key];
      const side = ag.x < .5 ? -1 : ag.x > .5 ? 1 : 0;
      return [Math.atan2(-a[0], a[2]) + side * .55, Math.atan2(a[1], Math.hypot(a[0], a[2])) + (.5 - ag.y) * .9];
    }

    // ---- interaction ----
    let last = null, moved = 0;
    canvas.addEventListener("pointerdown", (e) => {
      S.dragging = true; moved = 0; last = [e.clientX, e.clientY];
      canvas.setPointerCapture(e.pointerId); S.lastTouch = performance.now();
    });
    canvas.addEventListener("pointermove", (e) => {
      const r = canvas.getBoundingClientRect();
      const mx = e.clientX - r.left, my = e.clientY - r.top;
      if (S.dragging && last) {
        const dx = e.clientX - last[0], dy = e.clientY - last[1];
        moved += Math.abs(dx) + Math.abs(dy);
        S.vyaw = dx * .006; S.vpitch = dy * .006;
        S.yaw += S.vyaw; S.pitch = Math.max(-1.3, Math.min(1.3, S.pitch + S.vpitch));
        last = [e.clientX, e.clientY]; S.lastTouch = performance.now();
        tip.classList.remove("show");
        return;
      }
      let best = -1, bd = 20 * 20;
      S.proj.forEach((p, i) => { if (p[2] > -.15) { const d = (p[0] - mx) ** 2 + (p[1] - my) ** 2; if (d < bd) { bd = d; best = i; } } });
      S.hoverNode = best;
      canvas.style.cursor = best >= 0 ? "pointer" : "grab";
      if (best >= 0) {
        const k = owner[best], a = AGENTS[k];
        tip.innerHTML = `<b style="color:rgb(${col(k)})">${a.name}</b><span>${esc(history[k][0] || a.role)}</span>`;
        tip.style.left = `${mx}px`; tip.style.top = `${my}px`;
        tip.classList.add("show");
      } else tip.classList.remove("show");
    });
    canvas.addEventListener("pointerup", () => {
      S.dragging = false; last = null;
      if (moved < 5 && S.hoverNode >= 0) select(owner[S.hoverNode]);
    });
    canvas.addEventListener("pointercancel", () => { S.dragging = false; last = null; });
    canvas.addEventListener("pointerleave", () => { S.hoverNode = -1; tip.classList.remove("show"); });

    function select(key) {
      if (compact) { onSelect?.(key); ignite(key, 6); return; }
      S.selected = S.selected === key ? null : key;
      Object.entries(nodes).forEach(([k, n]) => n.el.classList.toggle("selected", k === S.selected));
      if (!S.selected) { detail.hidden = true; return; }
      renderDetail();
      S.lastTouch = -1e9;
      ignite(key, 6);
    }
    function renderDetail() {
      const key = S.selected, a = AGENTS[key];
      const items = history[key].slice(0, 8);
      detail.style.setProperty("--c", col(key).join(","));
      detail.innerHTML = `<div class="ad-head"><span class="ad-dot"></span><b>${a.name}</b><span class="ad-role">${a.role}</span>
        <button type="button" class="ad-close" aria-label="Close">×</button></div>
        ${items.length ? `<ol>${items.map((t) => `<li>${esc(t)}</li>`).join("")}</ol>` : `<p>No steps from this agent yet.</p>`}`;
      detail.querySelector(".ad-close").onclick = () => select(key);
      detail.hidden = false;
    }

    // ---- effects ----
    function ignite(key, n) {
      if (reduced || !AGENTS[key]) return;
      const start = anchorNode[key];
      for (let i = 0; i < n; i++) S.sparks.push({ key, from: start, to: M.nbrs[start][i % M.nbrs[start].length], t0: performance.now() + i * 90, hops: 5 + (i % 3) });
    }
    function ripple(key, rgb) { S.ripples.push({ o: anchors[key] || [0, -1, 0], rgb, t0: performance.now(), dur: 1400 }); }
    function send(key, n, inbound = true) {
      if (!S.cards[key] || reduced) return;
      const now = performance.now();
      for (let i = 0; i < n; i++) S.packets.push({ key, inbound, t0: now + i * 140, dur: 900 + Math.random() * 300 });
    }

    // ---- render ----
    function linkPath(key) {
      const card = S.cards[key];
      if (!card) return null;
      const p = S.proj[anchorNode[key]];
      let start;
      if (p && p[2] > -.2) start = [p[0], p[1]];
      else { const ang = Math.atan2(card[1] - S.cy, card[0] - S.cx); start = [S.cx + Math.cos(ang) * S.R, S.cy + Math.sin(ang) * S.R]; }
      const mx = (start[0] + card[0]) / 2;
      return key === "human"
        ? { p0: start, p1: [start[0], (start[1] + card[1]) / 2], p2: [card[0], (start[1] + card[1]) / 2], p3: card }
        : { p0: start, p1: [mx, start[1]], p2: [mx, card[1]], p3: card };
    }
    const bez = (p, t) => { const u = 1 - t; return [0, 1].map((i) => u * u * u * p.p0[i] + 3 * u * u * t * p.p1[i] + 3 * u * t * t * p.p2[i] + t * t * t * p.p3[i]); };

    function frame(now) {
      requestAnimationFrame(frame);
      if (document.hidden || !S.w) return;
      const dt = Math.min(.05, (now - (S.last || now)) / 1000);
      S.last = now;
      S.t += dt;
      const k = 1 - Math.pow(.03, dt);
      S.tint = mix(S.tint, S.tintTarget, k);
      S.energy += (S.energyTarget - S.energy) * k;
      S.ring += (S.ringTarget - S.ring) * k;
      S.beat *= Math.pow(.05, dt);

      // motion: drag inertia, then ease toward the agent in focus, otherwise a slow drift
      const idle = now - S.lastTouch > 3500;
      if (!S.dragging) {
        S.yaw += S.vyaw; S.pitch = Math.max(-1.3, Math.min(1.3, S.pitch + S.vpitch));
        S.vyaw *= .93; S.vpitch *= .93;
        const focusKey = S.hoverAgent || S.selected || S.active;
        if (idle && focusKey && !reduced) {
          const [ty, tp] = facing(focusKey);
          let dy = ty - S.yaw; dy = Math.atan2(Math.sin(dy), Math.cos(dy));
          S.yaw += dy * k * .8; S.pitch += (tp - S.pitch) * k * .8;
        } else if (idle && !reduced) {
          S.yaw += dt * .12;
          S.pitch += (-.25 - S.pitch) * k * .2;
        }
      }

      ctx.setTransform(S.dpr, 0, 0, S.dpr, 0, 0);
      ctx.clearRect(0, 0, S.w, S.h);
      S.proj = M.pts.map(project);

      // node intensity: region glow, selection, ripples, hover
      const focus = S.hoverAgent || S.active;
      const lit = M.pts.map((p, i) => {
        let v = 0, hue = null;
        if (focus) { v = smooth(.55, .97, dot(p, anchors[focus])) * (S.hoverAgent ? .8 : S.energy + .15); hue = col(focus); }
        if (S.selected && owner[i] === S.selected && .55 > v) { v = .55; hue = col(S.selected); }
        for (const r of S.ripples) {
          const u = (now - r.t0) / r.dur;
          const ang = Math.acos(Math.max(-1, Math.min(1, dot(p, r.o))));
          const g = Math.exp(-((ang - u * Math.PI) ** 2) / .03) * (1 - u);
          if (g > v) { v = g; hue = r.rgb; }
        }
        if (i === S.hoverNode) { v = Math.max(v, .9); hue = hue || col(owner[i]); }
        return [v, hue];
      });
      S.ripples = S.ripples.filter((r) => now - r.t0 < r.dur);

      const light = theme() === "light";
      // faint inner core
      const core = ctx.createRadialGradient(S.cx, S.cy, 0, S.cx, S.cy, S.R * 1.1);
      core.addColorStop(0, rgba(S.tint, (light ? .05 : .08) + S.energy * .05 + S.beat * .08));
      core.addColorStop(1, rgba(S.tint, 0));
      ctx.fillStyle = core; ctx.beginPath(); ctx.arc(S.cx, S.cy, S.R * 1.1, 0, Math.PI * 2); ctx.fill();

      // edges
      ctx.lineWidth = 1;
      for (const [a, b] of M.edges) {
        const pa = S.proj[a], pb = S.proj[b];
        const depth = ((pa[2] + pb[2]) / 2 + 1) / 2;
        const la = lit[a], lb = lit[b];
        const l = Math.min(la[0], lb[0]);
        const shade = l > .05 ? mix(S.tint, la[1] || lb[1], Math.min(1, l * 1.4)) : S.tint;
        ctx.strokeStyle = rgba(shade, (light ? .07 + depth * .2 : .03 + depth * .13) + l * .45);
        ctx.beginPath(); ctx.moveTo(pa[0], pa[1]); ctx.lineTo(pb[0], pb[1]); ctx.stroke();
      }
      // nodes
      S.proj.forEach((p, i) => {
        const depth = (p[2] + 1) / 2;
        const [l, c] = lit[i];
        const shade = l > .05 ? mix(S.tint, c, Math.min(1, l * 1.3)) : S.tint;
        const r = (.9 + depth * 1.3 + l * 1.6) * p[3];
        ctx.fillStyle = rgba(shade, (light ? .25 + depth * .55 : .12 + depth * .5) + l * .3);
        ctx.beginPath(); ctx.arc(p[0], p[1], r, 0, Math.PI * 2); ctx.fill();
      });
      // sparks hop along edges inside the region
      S.sparks = S.sparks.filter((s) => {
        const u = (now - s.t0) / 260;
        if (u < 0) return true;
        if (u >= 1) {
          if (--s.hops <= 0) return false;
          const opts = M.nbrs[s.to].filter((j) => j !== s.from && owner[j] === s.key);
          const pool = opts.length ? opts : M.nbrs[s.to];
          s.from = s.to; s.to = pool[Math.floor(Math.random() * pool.length)]; s.t0 = now;
          return true;
        }
        const a = S.proj[s.from], b = S.proj[s.to];
        const x = a[0] + (b[0] - a[0]) * u, y = a[1] + (b[1] - a[1]) * u;
        const c = col(s.key);
        ctx.strokeStyle = rgba(c, .5); ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(x, y); ctx.stroke();
        ctx.fillStyle = rgba(c, .95); ctx.beginPath(); ctx.arc(x, y, 1.8, 0, Math.PI * 2); ctx.fill();
        return true;
      });

      // links to the cards, and pulses along them
      if (S.linked) {
        for (const key of Object.keys(AGENTS)) {
          const p = linkPath(key);
          if (!p) continue;
          const c = col(key);
          const on = key === S.active || key === S.hoverAgent || key === S.selected;
          ctx.strokeStyle = on ? rgba(c, .55) : S.done.has(key) ? rgba(c, .22) : light ? "rgba(40,40,60,.08)" : "rgba(255,255,255,.05)";
          ctx.lineWidth = on ? 1.25 : 1;
          ctx.beginPath(); ctx.moveTo(...p.p0); ctx.bezierCurveTo(...p.p1, ...p.p2, ...p.p3); ctx.stroke();
        }
        S.packets = S.packets.filter((pk) => {
          const p = linkPath(pk.key);
          const u = (now - pk.t0) / pk.dur;
          if (!p || u > 1) return false;
          if (u < 0) return true;
          const e = u * u * (3 - 2 * u);
          const [x, y] = bez(p, pk.inbound ? 1 - e : e);
          const c = col(pk.key);
          const g = ctx.createRadialGradient(x, y, 0, x, y, 5);
          g.addColorStop(0, rgba(c, .9)); g.addColorStop(1, rgba(c, 0));
          ctx.fillStyle = g; ctx.beginPath(); ctx.arc(x, y, 5, 0, Math.PI * 2); ctx.fill();
          return true;
        });
      }

      // decision ring (human gate)
      if (S.ring > .01) {
        const rr = S.R * 1.28;
        ctx.strokeStyle = rgba(S.ringColor, .1 * S.ring); ctx.lineWidth = 1;
        ctx.beginPath(); ctx.arc(S.cx, S.cy, rr, 0, Math.PI * 2); ctx.stroke();
        ctx.strokeStyle = rgba(S.ringColor, .7 * S.ring); ctx.lineWidth = 1.5; ctx.lineCap = "round";
        const start = S.mood === "awaiting" ? S.t * 1.2 : -Math.PI / 2;
        const sweep = S.mood === "awaiting" ? Math.PI * .5 : Math.PI * 2 * Math.min(1, S.ring);
        ctx.beginPath(); ctx.arc(S.cx, S.cy, rr, start, start + sweep); ctx.stroke();
        ctx.lineCap = "butt";
      }
    }
    requestAnimationFrame(frame);

    // ---- behaviour ----
    function setActive(key) {
      S.active = key && AGENTS[key] ? key : null;
      Object.entries(nodes).forEach(([k, n]) => n.el.classList.toggle("active", k === S.active));
      S.tintTarget = S.mood === "alarm" ? P('BAD') : S.mood === "serene" ? P('CALM') : S.mood === "awaiting" ? mix(P('BASE'), P('GOLD'), .5) : P('BASE');
      S.energyTarget = S.active ? .85 : S.mood === "alarm" ? .5 : .3;
    }
    const status = (key, text) => { if (nodes[key]) nodes[key].status.textContent = text; };
    const result = (key, text, kind) => {
      if (!nodes[key]) return;
      nodes[key].result.textContent = text || "";
      nodes[key].result.className = `an-result ${kind || ""}`;
    };
    const markDone = (key) => { S.done.add(key); nodes[key]?.el.classList.add("done"); };
    const remember = (key, text) => { if (key && history[key]) { history[key].unshift(text); history[key].length = Math.min(history[key].length, 20); } };

    let typer = null;
    function say(key, text, fresh) {
      who.textContent = key ? AGENTS[key].name : "Argus";
      who.style.setProperty("--c", (key ? col(key) : P("WHITE")).join(","));
      clearInterval(typer);
      if (!fresh || reduced) { line.textContent = text; return; }
      let i = 0;
      line.textContent = "";
      typer = setInterval(() => { line.textContent = text.slice(0, (i += 2)); if (i >= text.length) clearInterval(typer); }, 12);
    }
    let toastTimer = null;
    function showToast(text, kind, fresh) {
      if (!fresh) return;
      toast.textContent = text;
      toast.className = `argus-toast show ${kind}`;
      clearTimeout(toastTimer);
      toastTimer = setTimeout(() => { toast.className = "argus-toast"; }, 2600);
    }
    const shortArgs = (args) => Object.values(args || {}).map((v) => typeof v === "string" ? v : JSON.stringify(v)).join(", ").slice(0, 48);

    function handle(e, fresh) {
      const p = e.payload || {};
      const key = ACTOR[e.actor];
      if (fresh) S.beat = Math.min(1, S.beat + .3);
      switch (e.type) {
        case "incident_opened":
          S.mood = "active"; S.done.clear(); S.ringTarget = 0;
          Object.values(history).forEach((h) => (h.length = 0));
          Object.entries(nodes).forEach(([k, n]) => { n.el.classList.remove("done", "active"); n.status.textContent = n.role; result(k, ""); });
          setActive("triage");
          say(null, `New problem reported: ${p.alert.title}. Starting to investigate.`, fresh);
          if (fresh) ripple("triage", P("WHITE"));
          break;
        case "rerun":
          S.mood = "active"; S.ringTarget = 0; container.classList.remove("awaiting");
          say(null, `Re-run requested by ${p.by}${p.note ? `: ${p.note}` : ""}`, fresh);
          break;
        case "attempt":
          if (p.n > 1) say(null, `Attempt ${p.n} of ${p.max}: ${p.stage === "fix" ? "trying another fix" : "looking for another cause"}.`, fresh);
          break;
        case "llm_step":
          if (key) {
            if (S.active !== key && fresh) send(key, 2, false);
            setActive(key);
            if (!p.tools?.length) status(key, "Thinking…");
          }
          break;
        case "tool_call": {
          if (key) setActive(key);
          const doing = (TOOL_TEXT[p.tool] || (() => `Using ${p.tool}`))(p.args || {});
          const first = String(p.result_preview || "").split("\n")[0].replace(/^[\w./:-]+:\d+:\s*/, "");
          const text = `${doing}. ${first.length > 90 ? first.slice(0, 90) + "…" : first}`;
          status(key, doing);
          remember(key, text);
          say(key, text, fresh);
          if (fresh) { send(key, 3); ignite(key, 4); }
          break;
        }
        case "rejected": {
          const fabricated = (p.errors || []).some((x) => /not found|out of range|unknown source/.test(x));
          status(key, fabricated ? "Quoted something that isn't in the logs" : "Answer sent back to fix");
          remember(key, `Checker rejected: ${(p.errors || [])[0] || ""}`);
          say(key, fabricated ? "Caught it quoting evidence that doesn't exist. Sent it back to try again." : "Its answer didn't pass the checks. Sent back to fix.", fresh);
          showToast(fabricated ? "Made-up evidence caught" : "Answer sent back", "bad", fresh);
          if (fresh && key) ripple(key, P('BAD'));
          break;
        }
        case "claim":
          markDone("triage");
          status("triage", `Possible cause ${p.id}`);
          remember("triage", `Possible cause ${p.id}: ${p.statement}`);
          say("triage", `Possible cause: ${p.statement}`, fresh);
          if (fresh) send("triage", 2);
          break;
        case "verdict": {
          setActive("verifier"); markDone("verifier");
          const kind = p.verdict === "supported" ? "good" : p.verdict === "refuted" ? "bad" : "warn";
          result("verifier", `${p.claim_id} ${VERDICT_TEXT[p.verdict].toLowerCase()}`, kind);
          status("verifier", p.reasoning.slice(0, 80));
          remember("verifier", `${p.claim_id} ${VERDICT_TEXT[p.verdict].toLowerCase()}: ${p.reasoning}`);
          say("verifier", `${VERDICT_TEXT[p.verdict]}: ${p.reasoning}`, fresh);
          showToast(`${p.claim_id} · ${VERDICT_TEXT[p.verdict]}`, kind, fresh);
          if (fresh) ripple("verifier", kind === "good" ? P('GOOD') : kind === "bad" ? P('BAD') : P('GOLD'));
          break;
        }
        case "suspects":
          markDone("code_history");
          result("code_history", p.suspects[0]?.sha || "", "");
          status("code_history", p.suspects[0] ? `Suspect: ${p.suspects[0].summary}` : "No suspicious changes");
          p.suspects.forEach((s) => remember("code_history", `Suspect ${s.sha}: ${s.summary}`));
          say("code_history", p.suspects[0] ? `Found a suspicious change: "${p.suspects[0].summary}" (${p.suspects[0].sha})` : "No suspicious changes found.", fresh);
          break;
        case "patch":
          markDone("fix"); setActive("sandbox");
          result("fix", p.fix_type, ""); result("sandbox", "", "");
          status("fix", p.rationale.slice(0, 80)); status("sandbox", "Testing the fix…");
          remember("fix", `Proposed ${p.fix_type}: ${p.rationale}`);
          say("fix", `Suggested a ${p.fix_type === "rollback" ? "rollback" : p.fix_type === "config_change" ? "settings change" : "code fix"}: ${p.rationale}`, fresh);
          if (fresh) { send("sandbox", 3, false); ignite("sandbox", 6); }
          break;
        case "sandbox": {
          markDone("sandbox");
          result("sandbox", p.passed ? "pass" : "fail", p.passed ? "good" : "bad");
          const b = p.before?.exit_code, a = p.after?.exit_code;
          status("sandbox", `Before: ${b === 0 ? "working" : "broken"} · After: ${a === undefined ? "not run" : a === 0 ? "working" : "broken"}`);
          const text = p.passed ? "The problem happens without the fix and goes away with it. All tests pass." : `The fix didn't work: ${p.error || "the problem still happens"}`;
          remember("sandbox", text);
          say("sandbox", text, fresh);
          showToast(p.passed ? "Fix works in testing" : "Fix didn't work", p.passed ? "good" : "bad", fresh);
          if (fresh) ripple("sandbox", p.passed ? P('GOOD') : P('BAD'));
          break;
        }
        case "awaiting_approval":
          S.mood = "awaiting"; S.ringTarget = 1; S.ringColor = P('GOLD');
          setActive("human"); S.energyTarget = .35;
          status("human", "Waiting for your approval");
          say("human", "A tested fix is ready. Nothing changes until a person approves it.", fresh);
          container.classList.add("awaiting");
          break;
        case "approval_vote":
          remember("human", `${p.approver} (${p.role}) ${p.decision}${p.reason ? `: ${p.reason}` : ""}`);
          status("human", `${p.approver} ${p.decision}`);
          say("human", `${p.approver} (${p.role}) ${p.decision} the fix.`, fresh);
          if (fresh) ripple("human", p.decision === "approved" ? P('GOOD') : P('BAD'));
          break;
        case "approval":
          container.classList.remove("awaiting");
          S.mood = p.approved ? "approved" : "rejected";
          S.ringColor = p.approved ? P('GOOD') : P('BAD'); S.ring = 0; S.ringTarget = 1;
          markDone("human");
          result("human", p.approved ? "approved" : "rejected", p.approved ? "good" : "bad");
          status("human", p.approver);
          say("human", `${p.approved ? "Approved" : "Rejected"} by ${p.approver}.`, fresh);
          showToast(p.approved ? "Approved" : "Rejected", p.approved ? "good" : "bad", fresh);
          if (fresh) ripple("human", p.approved ? P('GOOD') : P('BAD'));
          break;
        case "qa":
          say(null, `QA by ${p.tester}: ${p.result === "verified" ? "verified fixed" : "still failing"}`, fresh);
          showToast(p.result === "verified" ? "Verified by QA" : "QA: still failing", p.result === "verified" ? "good" : "bad", fresh);
          break;
        case "report":
          markDone("reporter"); setActive("reporter");
          result("reporter", "ready", "good");
          status("reporter", "Summary is ready to read");
          remember("reporter", "Wrote the incident summary.");
          say("reporter", "All done. The summary is ready in the Report tab.", fresh);
          setTimeout(() => { if (S.mood !== "awaiting") { S.mood = "serene"; S.ringTarget = 0; setActive(null); } }, fresh ? 2200 : 0);
          break;
        case "escalated":
          container.classList.remove("awaiting");
          S.mood = "alarm"; S.ringTarget = 1; S.ringColor = P('BAD');
          setActive(null);
          say(null, `Argus couldn't solve this one on its own and has handed it to a person. ${p.reason}`, fresh);
          showToast("Needs a person", "bad", fresh);
          break;
        case "error":
          say(null, p.message, fresh);
          break;
        case "phase":
          say(null, `${p.label}. ${p.detail}`, fresh);
          break;
        case "thinking":
          if (key) { setActive(key); status(key, `Thinking… step ${p.step} of ${p.max_steps}`); }
          break;
        case "waiting":
          say(key, p.reason === "busy" ? "The AI service is busy. Trying again in a moment." :
            `Pausing ${Math.round(p.seconds)}s. The free AI service only allows so much per minute.`, fresh);
          if (key) status(key, `Paused ${Math.round(p.seconds)}s (free-tier limit)`);
          break;
        case "failover":
          say(key, "Switched to another AI service to keep going.", fresh);
          break;
        case "comment":
          say(null, `${p.author}: ${p.text}`, fresh);
          break;
      }
      if (S.selected && key === S.selected) renderDetail();
    }

    layout();
    setActive(null);
    return {
      handle, setActive,
      setMood(m) { S.mood = m; setActive(S.active); },
      onSelect(fn) { onSelect = fn; },
      pulse(key) { if (AGENTS[key]) { ignite(key, 5); ripple(key, col(key)); } },
    };
  }

  return { mount, AGENTS };
})();
