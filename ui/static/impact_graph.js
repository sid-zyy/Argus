// Impact graph: the services and business operations an incident touches (observed vs inferred), the critical path
// to the money, how it spreads if nothing is done, and what each way out costs. A dark, pannable canvas with a
// toolbar, zoom, minimap and legend, a blast-radius side panel, and code / infrastructure / business layer cards.
// Data: GET /incidents/{id}/impact-graph.
const ImpactGraph = (() => {
  const NW = 196, NH = 62, COL = 252, ROW = 120, PAD = 36;
  const KIND = { external: "External", data: "Data store", infra: "Infrastructure", service: "Service", frontend: "Customer-facing", business: "Business" };
  const ICON = {
    data: '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
    external: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>',
    infra: '<rect x="5" y="5" width="14" height="14" rx="2"/><path d="M9.5 9.5h5v5h-5zM9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/>',
    service: '<circle cx="12" cy="12" r="3"/><path d="M12 2.5v4M12 17.5v4M2.5 12h4M17.5 12h4M5.3 5.3l2.8 2.8M15.9 15.9l2.8 2.8M5.3 18.7l2.8-2.8M15.9 8.1l2.8-2.8"/>',
    frontend: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18M6.5 6.5h.01M9.5 6.5h.01"/>',
    business: '<path d="M3 17l6-6 4 4 8-8"/><path d="M14 7h7v7"/>',
  };
  const svgIcon = (k, size = 18) => `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${ICON[k] || ICON.service}</svg>`;
  const rupees = (v) => v >= 1e5 ? `₹${(v / 1e5).toFixed(1)} L` : `₹${Math.round(v || 0).toLocaleString("en-IN")}`;
  const TOOL = {
    select: '<path d="M5 3l14 8-6 2-2 6z"/>',
    pan: '<path d="M8 13V5.5a1.5 1.5 0 0 1 3 0V12M11 11.5v-8a1.5 1.5 0 0 1 3 0V12M14 11.5V5a1.5 1.5 0 0 1 3 0v8.5c0 4-2.5 7.5-6.5 7.5S5 18 4 15.5l-1.4-3.6a1.5 1.5 0 0 1 2.7-1.2L8 15"/>',
    fit: '<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>',
    focus: '<circle cx="12" cy="12" r="3"/><path d="M3 12h3M18 12h3M12 3v3M12 18v3"/><circle cx="12" cy="12" r="8"/>',
  };
  const toolBtn = (k, title) => `<button type="button" class="igx-tool" data-tool="${k}" title="${title}"><svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${TOOL[k]}</svg></button>`;

  function layout(nodes) {
    const cols = Math.max(...nodes.map((n) => n.col)) + 1;
    const byCol = Array.from({ length: cols }, (_, c) => nodes.filter((n) => n.col === c));
    const rows = Math.max(...byCol.map((c) => c.length));
    const pos = {};
    byCol.forEach((list, c) => list.forEach((n, i) => { pos[n.id] = { x: PAD + c * COL, y: PAD + 18 + (rows - list.length) * ROW / 2 + i * ROW }; }));
    return { pos, W: PAD * 2 + (cols - 1) * COL + NW, H: PAD * 2 + 18 + (rows - 1) * ROW + NH };
  }

  function mount(el, iid) {
    let data = null, T = 0, touched = false, sel = null, focus = false, mode = "select";
    let view = { x: 0, y: 0, k: 1 }, world = { W: 1000, H: 400 }, fitted = false;
    el.innerHTML = `
      <div class="igx">
        <div class="igx-canvas" tabindex="0" aria-label="Impact graph">
          <div class="igx-toolbar">${toolBtn("select", "Select: click a part to see details")}${toolBtn("pan", "Pan: drag to move, scroll to zoom")}${toolBtn("fit", "Fit the whole graph")}${toolBtn("focus", "Show only the critical path")}</div>
          <div class="igx-zoom"><button type="button" data-z="-1" aria-label="Zoom out">−</button><span class="igx-pct">100%</span><button type="button" data-z="1" aria-label="Zoom in">+</button></div>
          <svg class="igx-svg"><defs>
            <filter id="igx-glow" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="2.2" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
            <marker id="igx-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0 10 5 0 10z" fill="context-stroke"/></marker>
          </defs><g class="igx-world"></g></svg>
          <div class="igx-mini"><svg class="igx-mini-svg"></svg></div>
          <div class="igx-legend">
            <div><i class="l-crit"></i>Critical path</div><div><i class="l-dep"></i>Dependency</div>
            <div><i class="l-inf"></i>Inferred spread</div><div><i class="l-root"></i>Root cause</div>
          </div>
          <div class="igx-time"><span>If nothing is done for <b class="igx-t">now</b></span><input type="range" min="0" max="90" step="1" class="igx-range" aria-label="Minutes if nothing is done"><span class="igx-proj"></span></div>
        </div>
        <aside class="igx-side"></aside>
      </div>
      <div class="igx-layers"></div>
      <div class="igx-ways"></div>
      <details class="impact-note igx-note"><summary>How this is worked out</summary><ul class="igx-assume"></ul></details>`;
    const q = (s) => el.querySelector(s);
    const canvas = q(".igx-canvas"), svg = q(".igx-svg"), gw = q(".igx-world"), range = q(".igx-range");

    // ---- view: pan and zoom ----
    function apply() {
      gw.setAttribute("transform", `translate(${view.x},${view.y}) scale(${view.k})`);
      q(".igx-pct").textContent = `${Math.round(view.k * 100)}%`;
      minimap();
    }
    function fit() {
      // fit between the toolbar and the legend / minimap / time bar, so nothing sits under them
      const w = canvas.clientWidth, top = 58, bottom = canvas.clientHeight - 172;
      if (!w || bottom <= top) return;
      const k = Math.min(1.3, (w - 24) / world.W, (bottom - top) / world.H);
      view = { k, x: (w - world.W * k) / 2, y: top + (bottom - top - world.H * k) / 2 };
      apply();
      fitted = true;
    }
    function zoomAt(f, cx, cy) {
      const k = Math.max(.35, Math.min(2.5, view.k * f));
      view.x = cx - (cx - view.x) * (k / view.k); view.y = cy - (cy - view.y) * (k / view.k); view.k = k;
      apply();
    }
    el.querySelectorAll("[data-z]").forEach((b) => b.onclick = () => zoomAt(+b.dataset.z > 0 ? 1.2 : 1 / 1.2, canvas.clientWidth / 2, canvas.clientHeight / 2));
    canvas.addEventListener("wheel", (e) => {
      if (!e.ctrlKey && !e.metaKey && mode !== "pan") return;  // plain scrolling still scrolls the page
      e.preventDefault();
      const r = canvas.getBoundingClientRect();
      zoomAt(e.deltaY < 0 ? 1.1 : 1 / 1.1, e.clientX - r.left, e.clientY - r.top);
    }, { passive: false });
    let drag = null;
    svg.addEventListener("pointerdown", (e) => {
      if (e.target.closest(".igx-node") && mode === "select") return;
      drag = { x: e.clientX, y: e.clientY, vx: view.x, vy: view.y };
      svg.setPointerCapture(e.pointerId);
      canvas.classList.add("dragging");
    });
    svg.addEventListener("pointermove", (e) => { if (drag) { view.x = drag.vx + e.clientX - drag.x; view.y = drag.vy + e.clientY - drag.y; apply(); } });
    svg.addEventListener("pointerup", () => { drag = null; canvas.classList.remove("dragging"); });
    el.querySelectorAll(".igx-tool").forEach((b) => b.onclick = () => {
      const t = b.dataset.tool;
      if (t === "fit") return fit();
      if (t === "focus") { focus = !focus; b.classList.toggle("on", focus); return paint(); }
      mode = t;
      canvas.dataset.mode = t;
      el.querySelectorAll('[data-tool="select"],[data-tool="pan"]').forEach((x) => x.classList.toggle("on", x.dataset.tool === t));
    });
    canvas.dataset.mode = mode;
    q('[data-tool="select"]').classList.add("on");
    range.oninput = () => { touched = true; T = +range.value; paint(); };
    addEventListener("resize", () => { if (data && canvas.clientWidth) fit(); });

    // ---- drawing ----
    // a fixed incident shows as recovered until the slider is moved; then it shows how far it would have spread
    const recovered = () => data.fixed && !touched;
    const failing = (n) => !recovered() && n.fails_at_min != null && n.fails_at_min <= T;
    // shorten a text element until it fits its width
    function fitText(el, max) {
      if (el.getComputedTextLength() <= max) return;
      let s = el.textContent;
      while (s.length > 3 && el.getComputedTextLength() > max) { s = s.slice(0, -1); el.textContent = `${s.trimEnd()}…`; }
    }
    function paint() {
      const L = layout(data.nodes);
      world = { W: L.W, H: L.H };
      const pos = L.pos, byId = Object.fromEntries(data.nodes.map((n) => [n.id, n]));
      const path = data.critical_path || [], onPath = new Set(path);
      const pathEdge = new Set(path.slice(1).map((b, i) => `${path[i]}>${b}`));
      const edges = data.edges.map((e) => {
        const a = pos[e.from], b = pos[e.to], x1 = a.x + NW + 4, y1 = a.y + NH / 2, x2 = b.x - 8, y2 = b.y + NH / 2, mx = (x1 + x2) / 2;
        const d = `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`;
        const live = failing(byId[e.from]) && failing(byId[e.to]);
        const crit = live && pathEdge.has(`${e.from}>${e.to}`);
        const cls = crit ? "crit" : live ? (e.observed ? "obs" : "inf") : "dep";
        const dim = focus && !crit ? " dim" : "";
        return `<path class="igx-edge ${cls}${dim}" d="${d}"${crit ? ' marker-end="url(#igx-arrow)" filter="url(#igx-glow)"' : ""}/>`
          + (crit && !dim ? `<circle class="igx-dot" r="2.6"><animateMotion dur="2.4s" repeatCount="indefinite" path="${d}"/></circle>` : "");
      }).join("");
      const nodes = data.nodes.map((n) => {
        const p = pos[n.id], on = failing(n), soon = !recovered() && !on && n.fails_at_min != null;
        const was = recovered() && n.status !== "healthy";
        const state = on ? (n.status === "observed" ? "obs" : "inf") : soon ? "soon" : was ? "rec" : "ok";
        const crit = on && onPath.has(n.id);
        const sub = on ? (n.value || (n.status === "observed" ? "Seen failing" : "Likely affected"))
          : soon ? `Fails in ${Math.round(n.fails_at_min - T)} min` : was ? "Recovered" : KIND[n.kind] || n.kind;
        const dim = focus && !onPath.has(n.id) ? " dim" : "";
        return `<g class="igx-node s-${state} k-${n.kind}${crit ? " crit" : ""}${n.origin ? " origin" : ""}${sel === n.id ? " sel" : ""}${dim}" data-id="${n.id}" transform="translate(${p.x},${p.y})" tabindex="0">
          <title>${esc(n.label)}: ${esc(n.note)}</title>
          <rect class="igx-box" width="${NW}" height="${NH}" rx="12"/>
          ${on || was ? `<rect class="igx-bar" x="0" y="12" width="3" height="${NH - 24}" rx="1.5"/>` : ""}
          <rect class="igx-ic" x="12" y="${(NH - 34) / 2}" width="34" height="34" rx="9"/>
          <g class="igx-icg" transform="translate(20,${(NH - 18) / 2})">${svgIcon(n.kind)}</g>
          <text class="igx-label" x="58" y="${NH / 2 - 3}">${esc(n.label)}</text>
          <text class="igx-sub" x="58" y="${NH / 2 + 15}">${esc(sub)}</text>
          ${n.origin ? `<text class="igx-root" x="0" y="-9">ROOT CAUSE</text>` : ""}
        </g>`;
      }).join("");
      gw.innerHTML = edges + nodes;
      gw.querySelectorAll(".igx-label, .igx-sub").forEach((t) => fitText(t, NW - 58 - 12));
      gw.querySelectorAll(".igx-node").forEach((g) => {
        g.onclick = () => { sel = sel === g.dataset.id ? null : g.dataset.id; paint(); side(); };
        g.onkeydown = (e) => { if (e.key === "Enter") g.onclick(); };
      });
      q(".igx-t").textContent = T ? `${T} min` : "now";
      const down = data.nodes.filter(failing);
      const next = data.nodes.filter((n) => n.fails_at_min != null && n.fails_at_min > T).sort((a, b) => a.fails_at_min - b.fails_at_min)[0];
      q(".igx-proj").innerHTML = recovered() ? `<span class="igx-ok">Fixed.</span> Drag to see how far it would have spread without the fix.`
        : `<b>${rupees(data.at_risk_per_minute * T)}</b> lost · <b>${down.length}/${data.nodes.length}</b> parts down${next ? ` · next: <b>${esc(next.label)}</b> in ${Math.round(next.fails_at_min - T)} min` : ""}`;
      if (!fitted) fit(); else apply();
    }
    function minimap() {
      if (!data) return;
      const m = q(".igx-mini-svg"), L = layout(data.nodes), s = Math.min(168 / L.W, 100 / L.H);
      const vw = canvas.clientWidth / view.k, vh = canvas.clientHeight / view.k, vx = -view.x / view.k, vy = -view.y / view.k;
      m.setAttribute("viewBox", `0 0 ${L.W * s} ${L.H * s}`);
      m.innerHTML = data.nodes.map((n) => { const p = L.pos[n.id]; return `<rect class="mm-${failing(n) ? (n.status === "observed" ? "obs" : "inf") : "ok"}" x="${p.x * s}" y="${p.y * s}" width="${NW * s}" height="${NH * s}" rx="2"/>`; }).join("")
        + `<rect class="mm-view" x="${vx * s}" y="${vy * s}" width="${vw * s}" height="${vh * s}" rx="3"/>`;
      m.onclick = (e) => {  // centre the view where the minimap was clicked
        const r = m.getBoundingClientRect(), wx = (e.clientX - r.left) / r.width * L.W, wy = (e.clientY - r.top) / r.height * L.H;
        view.x = canvas.clientWidth / 2 - wx * view.k; view.y = canvas.clientHeight / 2 - wy * view.k; apply();
      };
    }

    // ---- side panel: blast radius analysis, or the selected part ----
    function side() {
      const box = q(".igx-side"), byId = Object.fromEntries(data.nodes.map((n) => [n.id, n]));
      if (sel && byId[sel]) {
        const n = byId[sel], on = failing(n);
        const ups = data.edges.filter((e) => e.to === n.id).map((e) => byId[e.from].label);
        const downs = data.edges.filter((e) => e.from === n.id).map((e) => byId[e.to].label);
        box.innerHTML = `<div class="igx-side-head"><div><small>${esc(KIND[n.kind] || n.kind)}</small><h3>${esc(n.label)}</h3></div><button class="igx-x" aria-label="Back to the analysis">×</button></div>
          <span class="igx-chip ${on ? (n.status === "observed" ? "bad" : "warn") : n.fails_at_min != null ? "warn" : "ok"}">${on ? (n.status === "observed" ? "Failing · seen in the evidence" : "Affected · inferred from the map")
            : n.fails_at_min != null ? `Fails ${Math.round(n.fails_at_min)} min in, if nothing is done` : "Not affected"}</span>
          ${n.origin ? `<span class="igx-chip root">Root cause · the fix changes this</span>` : ""}
          <p class="igx-desc">${esc(n.note)}</p>
          ${n.value ? `<div class="igx-card"><small>Right now</small><b>${esc(n.value)}</b></div>` : ""}
          <div class="igx-card"><small>Depends on</small><div>${ups.map(esc).join(", ") || "Nothing in this map"}</div></div>
          <div class="igx-card"><small>If it fails, it affects</small><div>${downs.map(esc).join(", ") || "Nothing further"}</div></div>
          ${n.code.length ? `<div class="igx-card"><small>Code</small><div class="mono">${n.code.map(esc).join("<br>")}</div></div>` : ""}`;
        box.querySelector(".igx-x").onclick = () => { sel = null; paint(); side(); };
        return;
      }
      const path = (data.critical_path || []).map((id) => byId[id].label);
      const c = data.confidence, pi = data.potential_impact, L = data.layers.business, o = data.owner;
      const series = Array.from({ length: 19 }, (_, i) => data.nodes.filter((n) => n.fails_at_min != null && n.fails_at_min <= i * 5).length);
      const spark = series.map((v, i) => `${(i / 18) * 120},${34 - (v / data.nodes.length) * 32}`).join(" ");
      box.innerHTML = `<div class="igx-side-head"><h3>Blast radius analysis</h3></div>
        <span class="igx-chip ${data.fixed ? "ok" : path.length > 1 ? "bad" : "ok"}">${data.fixed ? "RESOLVED" : path.length > 1 ? "CRITICAL PATH DETECTED" : "NO CRITICAL PATH"}</span>
        ${path.length > 1 ? `<div class="igx-path">${path.map(esc).join(' <span>→</span> ')}</div>` : ""}
        <p class="igx-desc">${data.fixed ? `This was the path to the money. While it was broken, about <b>${rupees(data.at_risk_per_minute)}</b> was lost every minute.` : path.length > 1 ? `While this path is broken, about <b>${rupees(data.at_risk_per_minute)}</b> is lost every minute.` : "Nothing on the way to the business is failing right now."}</p>
        <div class="igx-card"><div class="igx-card-top"><small>Confidence</small><span class="igx-i" title="${esc(c.reasons.join(". "))}">?</span></div>
          <b class="igx-big">${c.value}%</b><div class="igx-meter"><i style="width:${c.value}%"></i></div>
          <small class="${c.value >= 80 ? "t-ok" : "t-warn"}">${c.value >= 80 ? "High" : c.value >= 60 ? "Medium" : "Low"} · ${esc(c.reasons[0] || "Early evidence only")}</small></div>
        <div class="igx-card igx-row"><div><small>Customers affected so far</small><b class="igx-big">${(L.affected || 0).toLocaleString("en-IN")}</b><small>${esc(L.unit)}</small></div>
          <svg viewBox="0 0 120 36" class="igx-spark" aria-label="Parts failing over 90 minutes if nothing is done"><polyline points="${spark}"/></svg></div>
        <div class="igx-card igx-row"><div><small>Owner</small><b>${esc(o.team)}</b><small>On call: ${esc(o.person || "nobody yet")}</small></div>
          <span class="igx-avatar">${esc((o.person || o.team).slice(0, 1))}</span></div>
        <div class="igx-card"><div class="igx-card-top"><small>Potential impact</small><span class="igx-chip sm ${pi.level === "High" ? "bad" : pi.level === "Medium" ? "warn" : "ok"}">${pi.level}</span></div>
          <div>${pi.business_parts.length ? `Hits ${pi.business_parts.map(esc).join(", ")}` : "No business operations affected yet"}</div></div>
        <div class="igx-actions"><button class="btn primary" data-go="live">Review the fix and decide</button><button class="btn" data-go="report">Report</button></div>`;
      box.querySelectorAll("[data-go]").forEach((b) => b.onclick = () => {
        if (window.showTab) window.showTab(b.dataset.go);
        if (b.dataset.go === "live") setTimeout(() => document.getElementById("approval-panel").scrollIntoView({ behavior: "smooth", block: "center" }), 50);
      });
    }

    // ---- layer cards and ways out ----
    function below() {
      const { code, infra, business } = data.layers;
      const card = (cls, icon, title, sub, stats) => `<div class="igx-layer ${cls}"><div class="igx-layer-head"><span class="igx-layer-ic">${icon}</span><div><b>${title}</b><small>${sub}</small></div></div>
        <div class="igx-layer-stats">${stats.map(([k, v, tone]) => `<div><small>${k}</small><b class="${tone || ""}">${v}</b></div>`).join("")}</div></div>`;
      q(".igx-layers").innerHTML =
        card("l-code", '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.8"><path d="m8 7-5 5 5 5M16 7l5 5-5 5M14 4l-4 16"/></svg>', "Code layer", "Where the fix goes",
          [["Files", code.files.length || "–"], ["Lines changed", code.lines || "–"], ["Tested", code.tested ? "Passed" : "Not yet", code.tested ? "t-ok" : "t-warn"], ["Bad commit", code.commit ? `<span class="mono">${esc(code.commit)}</span>` : "–"]])
        + card("l-infra", svgIcon("infra", 20), "Infrastructure layer", "Parts of the system",
          [["Mapped", infra.total], ["Seen failing", infra.observed, infra.observed ? "t-bad" : ""], ["Inferred", infra.inferred, infra.inferred ? "t-warn" : ""], ["Healthy", infra.healthy, "t-ok"]])
        + card("l-biz", svgIcon("business", 20), "Business layer", "What it costs",
          [["At risk", `${rupees(business.per_minute)}/min`, "t-bad"], ["Lost so far", rupees(business.so_far)], [`${esc(business.unit || "Units")} hit`, (business.affected || 0).toLocaleString("en-IN")], ["Manual fix", business.manual_minutes ? `~${business.manual_minutes} min` : "–"]]);
      q(".igx-ways").innerHTML = `<h3>Ways out</h3>` + (data.options.length ? data.options.map((o) => {
        const b = o.blast;
        return `<div class="ig-opt ${o.recommended ? "rec" : ""} o-${o.key}">
          <div class="ig-opt-top"><b>${esc(o.title)}</b>${o.recommended ? `<span class="ig-rec">Lowest cost</span>` : ""}</div>
          <div class="ig-opt-nums"><div><span>${rupees(o.loss)}</span><small>est. lost</small></div><div><span>~${o.minutes} min</span><small>to recover</small></div>
            ${b ? `<div><span class="lvl-${b.level.toLowerCase()}">${b.level}</span><small>blast radius ${b.score}/100</small></div>` : `<div><span>–</span><small>no change made</small></div>`}</div>
          ${b ? `<div class="ig-bar"><i style="width:${b.score}%" class="lvl-${b.level.toLowerCase()}"></i></div>
            <p class="ig-opt-why">Changes ${b.lines_changed} line${b.lines_changed === 1 ? "" : "s"} in ${b.files.map(esc).join(", ") || "–"}${b.tested ? ", tested in the sandbox" : ""}.
            ${b.reaches.length ? `Could affect ${b.reaches.length} part${b.reaches.length === 1 ? "" : "s"} downstream.` : ""}</p>` : ""}
          ${o.spreads_to.length ? `<p class="ig-opt-why warn">Meanwhile it spreads to: ${o.spreads_to.map(esc).join(", ")}.</p>` : ""}
          <p class="ig-opt-why">${esc(o.note)}</p></div>`;
      }).join("") : `<p class="small faint">Ways out appear once Argus has a tested fix.</p>`);
      q(".igx-assume").innerHTML = data.assumptions.map((a) => `<li>${esc(a)}</li>`).join("")
        + `<li>Confidence: ${esc(data.confidence.reasons.join("; ") || "early evidence only")}.</li>`
        + `<li>Red links join parts both seen failing in the ${esc(data.basis)}; amber dashed links are inferred from the service map. The critical path runs from the root cause to the money.</li>`;
    }

    async function load() {
      try { data = await api(`/incidents/${iid}/impact-graph`); } catch { data = null; return badge(); }
      if (!touched) { T = data.fixed ? 0 : Math.min(90, Math.round(data.minutes_so_far)); range.value = T; }
      paint(); side(); below(); badge();
    }
    function badge() {
      const b = document.getElementById("tb-impact");
      if (!b) return;
      const crit = data && (data.critical_path || []).length > 1 && !data.fixed;
      b.textContent = !data ? "" : crit ? "Critical" : data.fixed ? "Fixed" : "";
      b.className = `tb-badge ${crit ? "bad" : data && data.fixed ? "ok" : ""}`;
    }
    return { load, show() { fitted = false; if (data) requestAnimationFrame(() => paint()); } };  // labels can only be measured once visible
  }
  return { mount };
})();
