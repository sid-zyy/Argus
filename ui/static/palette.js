// The command palette (Ctrl/⌘ K), keyboard shortcuts (? for the list, g then d / r to jump), a confetti burst for
// fixed incidents, and a soft cursor spotlight on cards. Loaded on every page after common.js.

// ---- command palette ----
const PAL_ICON = {
  inc: '<svg viewBox="0 0 24 24"><path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/></svg>',
  go: '<svg viewBox="0 0 24 24"><path d="M5 12h14M13 6l6 6-6 6"/></svg>',
  play: '<svg viewBox="0 0 24 24"><path d="M7 5v14l12-7z"/></svg>',
  user: '<svg viewBox="0 0 24 24"><circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/></svg>',
  theme: '<svg viewBox="0 0 24 24"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>',
  pause: '<svg viewBox="0 0 24 24"><rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/></svg>',
  ask: '<svg viewBox="0 0 24 24"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>',
  tour: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="m15.5 8.5-2 5-5 2 2-5z"/></svg>',
  key: '<svg viewBox="0 0 24 24"><rect x="2" y="6" width="20" height="12" rx="2"/><path d="M6 10h.01M10 10h.01M14 10h.01M18 10h.01M7 14h10"/></svg>',
  shop: '<svg viewBox="0 0 24 24"><path d="M6 2 3 6v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6l-3-4z"/><path d="M3 6h18M16 10a4 4 0 0 1-8 0"/></svg>',
};
const isMac = /Mac|iPhone|iPad/.test(navigator.platform);
const MOD = isMac ? "⌘" : "Ctrl";

const Palette = {
  el: null, items: [], shown: [], sel: 0,
  mount() {
    const el = document.createElement("div");
    el.className = "pal-overlay";
    el.hidden = true;
    el.innerHTML = `<div class="pal" role="dialog" aria-modal="true" aria-label="Command palette">
      <div class="pal-search"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/></svg>
        <input class="pal-input" placeholder="Search incidents, or type a command" aria-label="Search" autocomplete="off" spellcheck="false"><kbd>Esc</kbd></div>
      <div class="pal-list" role="listbox"></div>
      <div class="pal-foot"><span><kbd>↑</kbd><kbd>↓</kbd> move</span><span><kbd>↵</kbd> run</span><span><kbd>?</kbd> all shortcuts</span></div></div>`;
    document.body.appendChild(el);
    this.el = el;
    this.input = el.querySelector(".pal-input");
    this.list = el.querySelector(".pal-list");
    el.onclick = (e) => { if (e.target === el) this.close(); };
    this.input.oninput = () => { this.sel = 0; this.render(); };
    this.input.onkeydown = (e) => {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        this.sel = (this.sel + (e.key === "ArrowDown" ? 1 : -1) + this.shown.length) % Math.max(1, this.shown.length);
        this.highlight();
      } else if (e.key === "Enter") { e.preventDefault(); this.run(this.shown[this.sel]); }
      else if (e.key === "Escape") { e.preventDefault(); this.close(); }
    };
    // a trigger in the top bar, like the search box in Linear or Vercel
    const spacer = document.querySelector(".topbar .spacer");
    if (spacer) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "pal-trigger";
      b.innerHTML = `<svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/></svg><span>Search or jump to…</span><kbd>${MOD} K</kbd>`;
      b.onclick = () => this.open();
      spacer.after(b);
    }
  },
  async commands() {
    const cmds = [];
    const add = (group, label, icon, run, hint = "", keys = "") => cmds.push({ group, label, icon, run, hint, keys });
    const [incidents, scenarios] = await Promise.all([api("/incidents").catch(() => []), api("/scenarios").catch(() => [])]);
    incidents.forEach((i) => add("Incidents", `${incKey(i.id)}  ${i.title}`, PAL_ICON.inc,
      () => (location.href = `/incidents/${i.id}/view`), STATUS_LABEL[i.status] || i.status));
    if (window.showTab) {
      [["live", "Live"], ["impact", "Impact graph"], ["findings", "Findings"], ["timeline", "Timeline"], ["report", "Report"]]
        .forEach(([t, l], n) => add("This incident", `Show ${l}`, PAL_ICON.go, () => window.showTab(t), "", String(n + 1)));
    }
    if (window.openAsk) add("This incident", "Ask Argus a question", PAL_ICON.ask, () => window.openAsk(), "", "/");
    add("Go to", "Dashboard", PAL_ICON.go, () => (location.href = "/"), "", "G D");
    add("Go to", "Results: Argus vs a chatbot", PAL_ICON.go, () => (location.href = "/results"), "", "G R");
    add("Go to", "Open ShopFast", PAL_ICON.shop, () => window.open("http://localhost:8100", "_blank", "noopener"));
    if (Session.user) {
      scenarios.filter((s) => !s.live_app).forEach((s) => {
        add("Start a demo incident", `${s.title}`, PAL_ICON.play, () => startDemo(s.name, "live"), s.service);
        if (s.replay_available) add("Start a demo incident", `Replay: ${s.title}`, PAL_ICON.play, () => startDemo(s.name, "replay"), "recorded, works offline");
      });
    }
    if (location.pathname === "/" && window.ArgusTour) add("Help", "Take the guided tour", PAL_ICON.tour, () => window.ArgusTour.start());
    add("Help", "Keyboard shortcuts", PAL_ICON.key, () => Shortcuts.open(), "", "?");
    Session.users.forEach((u) => {
      if (!Session.user || Session.user.id !== u.id) {
        add("Switch person", `${u.name}`, PAL_ICON.user, async () => { await Session.signIn(u.id); toastMsg(`You're now ${u.name} (${ROLE_LABEL[u.role]})`); }, ROLE_LABEL[u.role]);
      }
    });
    const dark = document.documentElement.dataset.theme === "dark";
    add("Settings", `Switch to ${dark ? "light" : "dark"} mode`, PAL_ICON.theme, () => document.getElementById("theme-btn")?.click());
    if (Session.is("developer", "lead")) {
      const p = ArgusState.state.paused;
      add("Settings", p ? "Resume Argus" : "Pause Argus (ignore monitoring alerts)", PAL_ICON.pause, () => ArgusState.set(!p).catch((e) => toastMsg(e.message, "bad")));
    }
    return cmds;
  },
  async open() {
    if (!this.el) return;
    this.el.hidden = false;
    requestAnimationFrame(() => this.el.classList.add("show"));
    this.input.value = "";
    this.sel = 0;
    this.input.focus();
    this.items = [];
    this.render();
    this.items = await this.commands();
    this.render();
  },
  close() {
    if (!this.el || this.el.hidden) return;
    this.el.classList.remove("show");
    setTimeout(() => (this.el.hidden = true), 150);
  },
  // fuzzy: every typed character in order; earlier and tighter matches rank first
  score(label, q) {
    if (!q) return 0;
    const s = label.toLowerCase();
    const at = s.indexOf(q);
    if (at >= 0) return 1000 - at;
    let i = 0, gaps = 0, last = -1;
    for (const ch of q) {
      const j = s.indexOf(ch, i);
      if (j < 0) return -1;
      if (last >= 0) gaps += j - last - 1;
      last = j; i = j + 1;
    }
    return 500 - gaps;
  },
  render() {
    const q = this.input.value.trim().toLowerCase();
    let items = this.items.map((c) => ({ c, s: this.score(`${c.label} ${c.hint} ${c.group}`, q) })).filter((x) => x.s >= 0);
    if (q) items.sort((a, b) => b.s - a.s);
    this.shown = items.map((x) => x.c).slice(0, 40);
    if (!this.items.length) { this.list.innerHTML = `<div class="pal-empty"><span class="pal-spin"></span>Loading…</div>`; return; }
    if (!this.shown.length) { this.list.innerHTML = `<div class="pal-empty">Nothing matches “${esc(this.input.value)}”.</div>`; return; }
    let group = "";
    this.list.innerHTML = this.shown.map((c, n) => {
      const head = !q && c.group !== group ? `<div class="pal-group">${esc((group = c.group))}</div>` : "";
      return `${head}<div class="pal-item" role="option" data-n="${n}">${c.icon}<span class="pal-label">${esc(c.label)}</span>
        ${c.hint ? `<span class="pal-hint">${esc(c.hint)}</span>` : ""}${c.keys ? `<span class="pal-keys">${c.keys.split(" ").map((k) => `<kbd>${esc(k)}</kbd>`).join("")}</span>` : ""}</div>`;
    }).join("");
    this.list.querySelectorAll(".pal-item").forEach((r) => {
      r.onmousemove = () => { if (this.sel !== +r.dataset.n) { this.sel = +r.dataset.n; this.highlight(false); } };
      r.onclick = () => this.run(this.shown[+r.dataset.n]);
    });
    this.highlight();
  },
  highlight(scroll = true) {
    this.list.querySelectorAll(".pal-item").forEach((r) => {
      const on = +r.dataset.n === this.sel;
      r.classList.toggle("on", on);
      r.setAttribute("aria-selected", on);
      if (on && scroll) r.scrollIntoView({ block: "nearest" });
    });
  },
  run(c) {
    if (!c) return;
    this.close();
    Promise.resolve(c.run()).catch((e) => toastMsg(e.message, "bad"));
  },
};
async function startDemo(name, mode) {
  location.href = (await api(`/demo/trigger/${encodeURIComponent(name)}?mode=${mode}`, { method: "POST" })).url;
}

// ---- keyboard shortcuts ----
const Shortcuts = {
  el: null,
  list() {
    const rows = [["General", [[`${MOD} K`, "Search and run commands"], ["?", "Show this list"], ["G D", "Go to the dashboard"], ["G R", "Go to results"], ["Esc", "Close a dialog"]]]];
    if (window.showTab) rows.push(["Incident page", [["1–5", "Switch tabs"], ["/", "Ask Argus"]]]);
    if (location.pathname === "/") rows.push(["Dashboard", [["N", "Start a demo incident"], ["T", "Take the guided tour"]]]);
    return rows;
  },
  open() {
    if (!this.el) {
      this.el = document.createElement("div");
      this.el.className = "pal-overlay";
      this.el.onclick = (e) => { if (e.target === this.el || e.target.closest(".ks-x")) this.close(); };
      document.body.appendChild(this.el);
    }
    this.el.innerHTML = `<div class="pal ks" role="dialog" aria-modal="true" aria-label="Keyboard shortcuts"><div class="ks-head"><b>Keyboard shortcuts</b><button class="ks-x" type="button" aria-label="Close">×</button></div>
      ${this.list().map(([g, keys]) => `<div class="pal-group">${esc(g)}</div>${keys.map(([k, what]) => `<div class="ks-row"><span>${esc(what)}</span><span class="pal-keys">${k.split(" ").map((x) => `<kbd>${esc(x)}</kbd>`).join("")}</span></div>`).join("")}`).join("")}</div>`;
    this.el.hidden = false;
    requestAnimationFrame(() => this.el.classList.add("show"));
  },
  close() { if (this.el) { this.el.classList.remove("show"); setTimeout(() => (this.el.hidden = true), 150); } },
  get isOpen() { return this.el && !this.el.hidden; },
};

let gPressed = 0;
document.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
    e.preventDefault();
    Palette.el && !Palette.el.hidden ? Palette.close() : Palette.open();
    return;
  }
  if (e.key === "Escape" && Shortcuts.isOpen) return Shortcuts.close();
  const typing = e.target.closest && e.target.closest("input, textarea, select, [contenteditable]");
  if (typing || e.metaKey || e.ctrlKey || e.altKey || document.querySelector("dialog[open]") || (Palette.el && !Palette.el.hidden)) return;
  if (e.key === "?") { e.preventDefault(); Shortcuts.isOpen ? Shortcuts.close() : Shortcuts.open(); return; }
  const k = e.key.toLowerCase();
  if (k === "g") { gPressed = Date.now(); return; }
  if (Date.now() - gPressed < 1200) {
    gPressed = 0;
    if (k === "d") location.href = "/";
    if (k === "r") location.href = "/results";
  }
});

// ---- confetti: a short burst when an incident is fixed ----
function confetti(opts = {}) {
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const cv = document.createElement("canvas");
  cv.className = "confetti";
  document.body.appendChild(cv);
  const dpr = devicePixelRatio || 1, W = innerWidth, H = innerHeight;
  cv.width = W * dpr; cv.height = H * dpr;
  const ctx = cv.getContext("2d");
  ctx.scale(dpr, dpr);
  const colors = ["#5048d8", "#2b9fc4", "#18794e", "#f0a33a", "#e0675c", "#9d93ea"];
  const ox = opts.x ?? W / 2, oy = opts.y ?? H / 3;
  const bits = Array.from({ length: opts.count || 140 }, () => {
    const a = -Math.PI / 2 + (Math.random() - .5) * Math.PI * 1.1, v = 7 + Math.random() * 9;
    return { x: ox, y: oy, vx: Math.cos(a) * v, vy: Math.sin(a) * v, r: Math.random() * Math.PI, vr: (Math.random() - .5) * .3,
      w: 6 + Math.random() * 6, h: 3 + Math.random() * 4, c: colors[(Math.random() * colors.length) | 0] };
  });
  const t0 = performance.now();
  (function frame(t) {
    const life = (t - t0) / 2600;
    ctx.clearRect(0, 0, W, H);
    bits.forEach((b) => {
      b.vy += .28; b.vx *= .985; b.vy *= .985; b.x += b.vx; b.y += b.vy; b.r += b.vr;
      ctx.save(); ctx.translate(b.x, b.y); ctx.rotate(b.r); ctx.globalAlpha = Math.max(0, 1 - life);
      ctx.fillStyle = b.c; ctx.fillRect(-b.w / 2, -b.h / 2, b.w, b.h * Math.abs(Math.cos(b.r * 2))); ctx.restore();
    });
    if (life < 1) requestAnimationFrame(frame); else cv.remove();
  })(t0);
}

// ---- cursor spotlight: cards light up softly under the pointer ----
document.addEventListener("pointermove", (e) => {
  const el = e.target.closest && e.target.closest(".tile, .card, .person, .how-step");
  if (!el) return;
  const r = el.getBoundingClientRect();
  el.style.setProperty("--mx", `${e.clientX - r.left}px`);
  el.style.setProperty("--my", `${e.clientY - r.top}px`);
}, { passive: true });

Palette.mount();
