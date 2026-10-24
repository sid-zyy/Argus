// Results page: Argus vs a single chatbot prompt on the same incidents (GET /results/data).
const $ = (id) => document.getElementById(id);
const DEMO = { incident1_conn_leak: "Checkout is failing", incident2_timeout: "Payments are timing out", incident3_cache_oom: "Product pages keep crashing" };
const CAPS = [
  ["Reads the logs, metrics and git history itself", true, false, "The chatbot only sees what someone pastes in"],
  ["Checks every quote against the real files", true, false, "Made-up evidence is blocked before anyone sees it"],
  ["Tries to prove its own guesses wrong", true, false, "A separate Fact checker reviews every cause"],
  ["Tests the fix on a safe copy first", true, false, "Replays the failure without and with the fix"],
  ["Waits for a person to approve", true, false, "A developer or team lead signs off before anything changes"],
  ["Starts by itself from a monitoring alert", true, false, "Prometheus Alertmanager webhook"],
  ["Keeps a full audit trail", true, false, "Every step, quote and decision is recorded"],
];
// the steps each one goes through; the race lights them in order, not to scale
const STEPS = { bot: ["Reads what was pasted", "Answers"], argus: ["Plans", "Investigates", "Fact-checks", "Finds the change", "Writes a fix", "Tests it"] };
const yes = '<span class="res-yes" aria-label="Yes">✓</span>', no = '<span class="res-no" aria-label="No">–</span>';
const pill = (ok, text) => `<span class="res-pill ${ok ? "ok" : "miss"}">${ok ? "✓" : "✗"} ${esc(text)}</span>`;
const RB = window.ReactBits;
const dark = () => document.documentElement.dataset.theme === "dark";
const calm = matchMedia("(prefers-reduced-motion: reduce)").matches;
const secs = (s) => s == null ? "–" : s < 60 ? `${s.toFixed(1)} s` : `${Math.floor(s / 60)} min ${String(Math.round(s % 60)).padStart(2, "0")} s`;

// the headline is real text from the first frame; its words just rise in
$("headline").innerHTML = $("headline").textContent.split(" ").map((w, i) => `<span style="--i:${i}">${esc(w)}</span>`).join(" ");
if (RB && !calm) {
  RB.particles($("particles"), {
    particleCount: 140, particleSpread: 12, speed: .06, particleBaseSize: 90, sizeRandomness: .8,
    particleColors: dark() ? ["#8b7bff", "#6ee7ff", "#ff7ad9"] : ["#5b4dd6", "#2b9fc4", "#c4479d"],
    alphaParticles: true, moveParticlesOnHover: true, particleHoverFactor: .6, connections: true, connectionDistance: 90,
    glow: .4, twinkle: .25, lightMode: !dark(), clickBurst: true,
  });
}

// run `fn` once, the first time `el` scrolls into view
function onSeen(el, fn) {
  if (calm || !("IntersectionObserver" in window)) return fn();
  const io = new IntersectionObserver(([e]) => { if (e.isIntersecting) { io.disconnect(); fn(); } }, { threshold: .25 });
  io.observe(el);
}

function card(el, n, of, suffix, label, sub) {
  const html = `<div class="res-stat"><div class="res-num"><span class="cu"></span>${of ? `<span class="res-of"> of ${of}</span>` : ""}</div>
    <div class="res-label">${esc(label)}</div><div class="res-sub">${sub}</div></div>`;
  const box = document.createElement("div");
  el.replaceWith(box);
  if (!RB) { box.innerHTML = html.replace('<span class="cu"></span>', `${n}${suffix}`); return; }
  RB.spotlight(box, { html, className: "res-card", theme: dark() ? "dark" : "light", spotlightColor: dark() ? "rgba(139,123,255,.25)" : "rgba(91,77,214,.14)" });
  requestAnimationFrame(() => { const cu = box.querySelector(".cu"); if (cu) RB.countUp(cu, { to: n, duration: 1.6, suffix }); });
}

// ---- head to head: a pair of bars per measure, including the one the chatbot wins ----
function renderScore(rows) {
  const n = rows.length, sum = (who, k) => rows.reduce((s, r) => s + ((r[who] && r[who][k]) || 0), 0);
  const count = (who, k) => rows.filter((r) => r[who] && r[who][k]).length;
  const pct = (who) => { const t = sum(who, "evidence_total"); return t ? Math.round((sum(who, "evidence_verified") / t) * 100) : 0; };
  const avg = (who) => sum(who, "seconds") / Math.max(1, rows.filter((r) => r[who] && r[who].seconds != null).length);
  const M = [
    { k: "Quotes that match the real files", a: pct("argus"), c: pct("chatbot"), max: 100, fmt: (v) => `${v}%`,
      why: `${sum("argus", "evidence_verified")} of ${sum("argus", "evidence_total")} for Argus, ${sum("chatbot", "evidence_verified")} of ${sum("chatbot", "evidence_total")} for the chatbot` },
    { k: "Culprit change named", a: count("argus", "named_culprit"), c: count("chatbot", "named_culprit"), max: n, fmt: (v) => `${v} of ${n}`,
      why: "The exact commit or setting that caused it" },
    { k: "Fix works when replayed", a: count("argus", "fix_passes"), c: count("chatbot", "fix_passes"), max: n, fmt: (v) => `${v} of ${n}`,
      why: "Argus tests its own fix before asking; we tested the chatbot's fixes for it in the same sandbox" },
    { k: "Own mistakes caught", a: sum("argus", "fabrications_blocked") + count("argus", "wrong_guess_caught"), c: 0, max: 0, fmt: (v) => String(v),
      why: "Made-up quotes blocked and wrong guesses ruled out before anyone acted on them" },
    { k: "Average time to an answer", a: avg("argus"), c: avg("chatbot"), max: 0, fmt: secs, lower: true,
      why: "The chatbot is much faster. Argus spends the time reading, checking and testing" },
  ];
  $("score-rows").innerHTML = M.map((m) => {
    const top = m.max || Math.max(m.a, m.c, 1), win = m.a === m.c ? "" : (m.lower ? m.a < m.c : m.a > m.c) ? "argus" : "bot";
    const bar = (who, v) => `<div class="sc-bar ${who}${win === who ? " win" : ""}"><span class="sc-track"><i style="--w:${v ? Math.max(2, (v / top) * 100) : 0}%"></i></span><b>${esc(m.fmt(v))}</b></div>`;
    const badge = win ? `<span class="sc-win ${win}">${win === "argus" ? "Argus" : "Chatbot"} ${m.lower ? "faster" : "ahead"}</span>` : `<span class="sc-win tie">Tie</span>`;
    return `<div class="sc-row"><div class="sc-what"><b>${esc(m.k)}</b>${badge}<small>${esc(m.why)}</small></div>
      <div class="sc-bars">${bar("argus", m.a)}${bar("bot", m.c)}</div></div>`;
  }).join("");
  onSeen($("score"), () => $("score").classList.add("go"));
}

// ---- incident by incident: both answer the same alert, played back on one clock ----
function renderRows(rows) {
  $("rows").innerHTML = rows.map((r, i) => {
    const a = r.argus || {}, c = r.chatbot || {};
    const ev = (x) => x.evidence_total ? `${x.evidence_verified} of ${x.evidence_total} quotes real` : "No quotes";
    const lane = (who, x, name) => `<div class="race-lane ${who}"><div class="race-who">${name}</div>
        <div class="race-track"><i class="race-fill"></i></div><div class="race-time" data-final="${esc(secs(x.seconds))}">${esc(secs(x.seconds))}</div>
        <div class="race-steps">${STEPS[who].map((s) => `<span>${esc(s)}</span>`).join("")}</div></div>`;
    const side = (who, x, name, extra) => `<div class="res-side ${who}"><div class="res-who">${name}</div>
        <p class="res-cause">${esc(x.root_cause || "Not run yet")}</p>${(x.root_cause || "").length > 220 ? `<button class="res-more" type="button">Show the full answer</button>` : ""}
        <div class="res-pills">${pill(x.named_culprit, "Named the culprit")}${pill(x.evidence_total && x.evidence_verified === x.evidence_total, ev(x))}${pill(x.fix_passes, "Fix passed")}${extra}</div></div>`;
    return `<section class="panel res-row" data-i="${i}">
      <div class="res-row-head"><h3>${esc(DEMO[r.scenario] || r.title)}</h3><span class="small faint">${esc(r.title)}</span><span class="spacer"></span>
        <button class="btn small race-play" type="button"><svg viewBox="0 0 24 24"><path d="M7 5v14l12-7z"/></svg>Play again</button>
        <button class="btn small ghost race-open" type="button" data-s="${esc(r.scenario)}" title="Start a recorded Argus run of this incident and watch every step">Watch the recorded run</button></div>
      <div class="race">${lane("bot", c, "Chatbot")}${lane("argus", a, "Argus")}<div class="race-note">Same clock for both, sped up. Steps light up in order, not to scale.</div></div>
      <div class="res-row-grid">${side("argus", a, "Argus", a.wrong_guess_caught ? pill(true, "Caught a wrong guess") : "")}${side("bot", c, "Chatbot", "")}</div></section>`;
  }).join("");
  $("rows").querySelectorAll(".res-row").forEach((row) => {
    const r = rows[+row.dataset.i];
    row.querySelector(".race-play").onclick = () => race(row, r);
    row.querySelector(".race-open").onclick = (e) => {
      if (!Session.user) return toastMsg("Choose who you are (top right) first", "bad");
      startDemo(e.currentTarget.dataset.s, "replay").catch((err) => toastMsg(err.message, "bad"));
    };
    row.querySelectorAll(".res-more").forEach((b) => b.onclick = () => {
      const open = b.previousElementSibling.classList.toggle("open");
      b.textContent = open ? "Show less" : "Show the full answer";
    });
    onSeen(row, () => race(row, r));
  });
}
// The slower run always plays in about 4 seconds and the other runs on the same clock, so the gap in time stays true.
function race(row, r) {
  const a = (r.argus && r.argus.seconds) || 0, c = (r.chatbot && r.chatbot.seconds) || 0, top = Math.max(a, c, 1);
  const msPerSec = calm ? 0 : 4000 / top;
  const lanes = [["bot", c], ["argus", a]].map(([who, s]) => ({ who, s, el: row.querySelector(`.race-lane.${who}`) }));
  row.classList.remove("done-bot", "done-argus");
  const t0 = performance.now();
  cancelAnimationFrame(row._raf);
  (function tick(now) {
    const t = msPerSec ? (now - t0) / msPerSec : top;  // seconds on the shared clock
    let running = false;
    lanes.forEach(({ who, s, el }) => {
      const p = s ? Math.min(1, t / s) : 1;
      el.querySelector(".race-fill").style.width = `${(Math.min(t, s) / top) * 100}%`;
      const time = el.querySelector(".race-time");
      time.textContent = p < 1 ? secs(t) : time.dataset.final;
      const steps = el.querySelectorAll(".race-steps span");
      steps.forEach((sp, k) => sp.classList.toggle("on", p * steps.length > k));
      if (p >= 1) row.classList.add(`done-${who}`); else running = true;
    });
    row.classList.toggle("racing", running);
    if (running) row._raf = requestAnimationFrame(tick);
  })(t0);
}

api("/results/data").then((d) => {
  const rows = d.rows, n = rows.length;
  const A = (k) => rows.filter((r) => r.argus && r.argus[k]).length;
  const C = (k) => rows.filter((r) => r.chatbot && r.chatbot[k]).length;
  const q = (who, k) => rows.reduce((s, r) => s + ((r[who] && r[who][k]) || 0), 0);
  const aq = q("argus", "evidence_verified"), at = q("argus", "evidence_total");
  const cq = q("chatbot", "evidence_verified"), ct = q("chatbot", "evidence_total");
  const pct = (a, b) => (b ? Math.round((a / b) * 100) : 0);
  const blocked = q("argus", "fabrications_blocked"), caught = A("wrong_guess_caught");
  const slots = [...$("stats").children];
  card(slots[0], pct(aq, at), 0, "%", "Of quoted evidence is real",
    `${aq} of ${at} quotes match the real files. Chatbot: <b>${pct(cq, ct)}%</b> (${cq} of ${ct}).`);
  card(slots[1], A("named_culprit"), n, "", "Culprit changes named",
    `Found the exact commit or setting every time. Chatbot: <b>${C("named_culprit")} of ${n}</b>.`);
  card(slots[2], A("fix_passes"), n, "", "Fixes proven before asking",
    "Each fix replayed against the failure in a sandbox, then sent for approval.");
  card(slots[3], blocked + caught, 0, "", "Mistakes caught by itself",
    `${blocked} made-up quote blocked and ${caught} wrong guess ruled out before anyone acted on them.`);

  renderScore(rows);
  $("caps").innerHTML = `<thead><tr><th></th><th>Chatbot</th><th class="res-argus-col">Argus</th></tr></thead><tbody>` +
    CAPS.map(([what, a, c, why], i) => `<tr style="--i:${i}"><td><b>${esc(what)}</b><div class="small faint">${esc(why)}</div></td><td>${c ? yes : no}</td><td class="res-argus-col">${a ? yes : no}</td></tr>`).join("") + "</tbody>";
  onSeen($("caps"), () => $("caps").classList.add("go"));
  renderRows(rows);

  const models = (who) => [...new Set(rows.map((r) => r[who] && r[who].model).filter(Boolean).join(", ").split(", "))].join(", ") || "–";
  $("note").textContent = `How this was measured: one recorded Argus run per incident (models: ${models("argus")}). The chatbot got one prompt per incident (model: ${models("chatbot")}) with what an engineer would usually paste: the alert, the end of each log, the deploy log and the latest code change, and no tools. Its fixes were tested by us in the same sandbox.${d.baseline_recorded_at ? ` Chatbot run on ${d.baseline_recorded_at}.` : ""}`;
}).catch((e) => toastMsg(e.message, "bad"));

mountThemeToggle($("theme-btn"));
mountUserMenu($("user-menu"));
Session.load();
