// A short guided tour of the dashboard: a spotlight on one part at a time with a card that explains it,
// like the product tours in Linear or Intercom. Starts from the "Take the tour" button, T, or the command palette.
const TOUR_STEPS = [
  { el: ".dash-head", title: "Meet Argus", text: "An AI incident commander. It finds the cause of an outage, tests a fix on a safe copy, and asks a person before changing anything." },
  { el: "#mind-panel", title: "The agent team, live", text: "Seven agents with one job each. When an incident is running, the sphere shows who is working: Athena plans, Theia investigates, Themis tries to prove each guess wrong, Hephaestus writes the fix." },
  { el: "#tiles", title: "What it's worth", text: "Revenue protected, engineer time saved, and how many wrong guesses and made-up quotes were caught before anyone acted on them." },
  { el: "#svc-health", title: "Service health", text: "Thirty days per service, like a public status page. Hover a day to see what broke and how long it took to fix." },
  { el: "#live-app", title: "A real app to break", text: "ShopFast is a real shop with a real git repo. Push a bad change and Argus notices on its own, reads the real logs and commits a fix after you approve it." },
  { el: "#board", title: "Every incident, start to finish", text: "Cards move from Investigating to Needs approval to Fixed. Open timers tick live, and nothing reaches production without a person." },
  { el: ".pal-trigger", title: "Jump anywhere", text: `Press ${/Mac|iPhone|iPad/.test(navigator.platform) ? "⌘" : "Ctrl"} K to open an incident, start a demo, switch person or pause Argus. Press ? for every shortcut.` },
  { el: "#trigger-btn", title: "Your turn", text: "Start a demo incident and watch the agents work it out in real time.", cta: "Start a demo" },
];

const ArgusTour = {
  i: 0, steps: [], root: null,
  start() {
    this.steps = TOUR_STEPS.filter((s) => { const el = document.querySelector(s.el); return el && el.offsetParent !== null; });
    if (!this.steps.length) return;
    if (!this.root) {
      this.root = document.createElement("div");
      this.root.className = "tour";
      this.root.innerHTML = `<div class="tour-hole"></div><div class="tour-card" role="dialog" aria-live="polite"></div>`;
      document.body.appendChild(this.root);
      this.root.addEventListener("click", (e) => { if (e.target === this.root) this.end(); });
      addEventListener("resize", () => this.root.hidden || this.place());
      addEventListener("scroll", () => this.root.hidden || this.place(), { passive: true });
      document.addEventListener("keydown", (e) => {
        if (this.root.hidden) return;
        if (e.key === "Escape") this.end();
        else if (e.key === "ArrowRight" || e.key === "Enter") { e.preventDefault(); this.go(this.i + 1); }
        else if (e.key === "ArrowLeft") this.go(this.i - 1);
      });
    }
    this.root.hidden = false;
    requestAnimationFrame(() => this.root.classList.add("show"));
    this.go(0);
  },
  go(i) {
    if (i >= this.steps.length) return this.end();
    this.i = Math.max(0, i);
    const s = this.steps[this.i], last = this.i === this.steps.length - 1;
    document.querySelector(s.el).scrollIntoView({ block: "center", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
    const card = this.root.querySelector(".tour-card");
    card.innerHTML = `<div class="tour-top"><span class="tour-n">${this.i + 1} of ${this.steps.length}</span><button class="tour-x" type="button" aria-label="End the tour">×</button></div>
      <h3>${esc(s.title)}</h3><p>${esc(s.text)}</p>
      <div class="tour-foot"><div class="tour-dots">${this.steps.map((_, n) => `<i class="${n === this.i ? "on" : n < this.i ? "past" : ""}"></i>`).join("")}</div>
        ${this.i ? `<button class="btn small ghost" data-t="back">Back</button>` : ""}
        <button class="btn small primary" data-t="next">${last ? esc(s.cta || "Done") : "Next"}</button></div>`;
    card.querySelector(".tour-x").onclick = () => this.end();
    card.querySelectorAll("[data-t]").forEach((b) => b.onclick = (e) => {
      e.stopPropagation();
      if (b.dataset.t === "back") return this.go(this.i - 1);
      if (last && s.cta) { this.end(); setTimeout(() => document.querySelector(s.el).click(), 50); return; }
      this.go(this.i + 1);
    });
    card.classList.remove("in");
    void card.offsetWidth;
    card.classList.add("in");
    this.place();
    setTimeout(() => this.place(), 450);  // again once the smooth scroll has settled
    card.querySelector("[data-t=next]").focus({ preventScroll: true });
  },
  place() {
    const s = this.steps[this.i], el = document.querySelector(s.el);
    if (!el) return;
    const r = el.getBoundingClientRect(), pad = 8;
    Object.assign(this.root.querySelector(".tour-hole").style,
      { left: `${r.left - pad}px`, top: `${r.top - pad}px`, width: `${r.width + pad * 2}px`, height: `${r.height + pad * 2}px` });
    const card = this.root.querySelector(".tour-card"), cw = card.offsetWidth, ch = card.offsetHeight, gap = 16;
    const fitY = (t) => Math.min(innerHeight - ch - 12, Math.max(12, t));
    let top = r.bottom + pad + gap, left = r.left + r.width / 2 - cw / 2;
    if (top + ch > innerHeight - 12) top = r.top - pad - gap - ch;          // above when there is no room below
    if (top < 12) {                                                          // tall targets: beside them, else over their top
      top = fitY(r.top + r.height / 2 - ch / 2);
      if (r.right + pad + gap + cw < innerWidth - 12) left = r.right + pad + gap;
      else if (r.left - pad - gap - cw > 12) left = r.left - pad - gap - cw;
      else top = fitY(r.top + 12);
    }
    left = Math.min(innerWidth - cw - 16, Math.max(16, left));
    Object.assign(card.style, { left: `${left}px`, top: `${top}px` });
  },
  end() {
    if (!this.root) return;
    this.root.classList.remove("show");
    setTimeout(() => (this.root.hidden = true), 250);
    store.set("argus.toured", "1");
  },
};
window.ArgusTour = ArgusTour;
