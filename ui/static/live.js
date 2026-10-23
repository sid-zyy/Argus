// Live progress panel: what Argus is doing right now, in plain words, so a run never looks frozen.
// Fed with every audit-log event (history first, then live); a 1 s ticker keeps timers and countdowns moving.
const LiveProgress = (() => {
  const PROVIDER = { groq: "Groq", gemini: "Gemini", ollama: "the local model" };
  const ORDER = ["investigate", "verify", "history", "fix", "test", "approval", "report"];
  const LABEL = { investigate: "Looking into it", verify: "Double-checking", history: "Checking recent changes",
    fix: "Writing a fix", test: "Testing the fix", approval: "Waiting for approval", report: "Writing the summary" };
  const ICON_DONE = '<svg viewBox="0 0 24 24"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>';

  const fmt = (ms) => {
    const s = Math.max(0, Math.round(ms / 1000));
    return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
  };

  function mount(el) {
    el.innerHTML = `
      <div class="live-main">
        <div class="live-head"><span class="live-dot"></span><span class="live-phase">Waiting to start</span><span class="live-timer"></span></div>
        <p class="live-detail"></p>
        <div class="live-agent" hidden></div>
        <div class="live-thought" hidden></div>
        <div class="live-wait" hidden><div class="live-wait-text"></div><div class="live-wait-bar"><span></span></div></div>
        <ol class="live-actions"></ol>
      </div>
      <div class="live-side"><div class="live-side-title">Progress</div><ol class="live-phases"></ol><div class="live-total"></div></div>`;
    const q = (s) => el.querySelector(s);
    const S = { phases: [], agent: null, step: 0, max: 0, stepStart: null, thought: "", wait: null, actions: [], done: null, started: null };

    const now = () => Date.now();
    const t = (e) => Date.parse(e.ts);

    function addAction(text, kind = "", ts = now()) {
      S.actions.unshift({ text, kind, ts });
      S.actions.length = Math.min(S.actions.length, 6);
    }

    function handle(e) {
      const p = e.payload || {};
      const who = AGENT_NAME[e.actor] || e.actor;
      switch (e.type) {
        case "incident_opened":
          S.started = t(e); S.done = null; S.phases = []; S.actions = []; S.agent = null; S.thought = ""; S.wait = null;
          break;
        case "rerun":
          S.done = null; S.wait = null;
          addAction(`${p.by} asked Argus to try again`, "", t(e));
          break;
        case "phase": {
          const last = S.phases[S.phases.length - 1];
          if (last && !last.end) last.end = t(e);
          S.phases.push({ key: p.key, label: p.label, detail: p.detail, start: t(e), end: null, attempt: p.attempt });
          S.agent = null; S.thought = ""; S.wait = null;
          break;
        }
        case "thinking":
          S.agent = e.actor; S.step = p.step; S.max = p.max_steps; S.stepStart = t(e);
          break;
        case "llm_step":
          S.wait = null;
          if (p.thought) S.thought = p.thought;
          if (p.provider) S.provider = p.provider;
          break;
        case "waiting":
          S.wait = { until: t(e) + p.seconds * 1000, total: p.seconds * 1000, provider: p.provider, reason: p.reason };
          break;
        case "failover":
          addAction(`${PROVIDER[p.provider] || p.provider} ${p.reason}, so Argus switched to another AI service`, "warn", t(e));
          break;
        case "tool_call":
          addAction(`${who} ${describeEvent(e)}`, "", t(e));
          break;
        case "rejected": case "claim": case "verdict": case "suspects": case "patch": case "sandbox":
          addAction(`${who} ${describeEvent(e)}`, e.type === "rejected" || (e.type === "verdict" && p.verdict === "refuted") || (e.type === "sandbox" && !p.passed) ? "bad"
            : (e.type === "verdict" && p.verdict === "supported") || (e.type === "sandbox" && p.passed) ? "good" : "key", t(e));
          break;
        case "awaiting_approval":
          S.agent = null; S.wait = null;
          break;
        case "approval":
          addAction(describeEvent(e), p.approved ? "good" : "bad", t(e));
          break;
        case "report": case "escalated":
          if (S.phases.length && !S.phases[S.phases.length - 1].end) S.phases[S.phases.length - 1].end = t(e);
          S.done = { ts: t(e), kind: e.type === "report" ? "ok" : "stop", reason: p.reason };
          S.agent = null; S.wait = null;
          if (e.type === "escalated") addAction(`Handed to a person: ${p.reason}`, "bad", t(e));
          break;
      }
    }

    function render() {
      const cur = S.phases[S.phases.length - 1];
      const running = !S.done && cur && cur.key !== "approval";
      el.classList.toggle("is-running", !!running);
      el.classList.toggle("is-waiting", !!S.wait && S.wait.until > now());
      // headline
      if (S.done) {
        q(".live-phase").textContent = S.done.kind === "ok" ? "All done" : "Stopped: needs a person";
        q(".live-detail").textContent = S.done.kind === "ok" ? "Argus finished. The summary is in the Summary report tab." : S.done.reason || "";
        q(".live-timer").textContent = S.started ? `took ${fmt(S.done.ts - S.started)}` : "";
      } else if (cur) {
        q(".live-phase").textContent = cur.attempt > 1 ? `${cur.label} (try ${cur.attempt})` : cur.label;
        q(".live-detail").textContent = cur.detail;
        q(".live-timer").textContent = cur.key === "approval" ? `waiting ${fmt(now() - cur.start)}` : fmt(now() - cur.start);
      } else {
        q(".live-phase").textContent = S.started ? "Getting started" : "Waiting to start";
        q(".live-detail").textContent = "";
        q(".live-timer").textContent = "";
      }
      // who is working
      const ag = q(".live-agent");
      ag.hidden = !S.agent || !!S.done;
      if (S.agent && !S.done) {
        const c = (Argus.AGENTS[S.agent] || {})[document.documentElement.dataset.theme === "dark" ? "dark" : "light"] || [90, 90, 120];
        ag.style.setProperty("--c", c.join(","));
        ag.innerHTML = `<span class="la-dot"></span><b>${esc(AGENT_NAME[S.agent] || S.agent)}</b>
          <span>is thinking · step ${S.step} of ${S.max}${S.provider ? ` · using ${esc(PROVIDER[S.provider] || S.provider)}` : ""}</span>
          <span class="la-time">${S.stepStart ? fmt(now() - S.stepStart) : ""}</span>`;
      }
      // the model's own words
      const th = q(".live-thought");
      const thought = S.thought.replace(/\s+/g, " ").trim();
      th.hidden = !thought || !!S.done;
      th.textContent = thought.length > 260 ? "…" + thought.slice(-260) : thought;
      // free-tier waits, with a countdown
      const w = q(".live-wait");
      const waiting = S.wait && S.wait.until > now();
      w.hidden = !waiting;
      if (waiting) {
        const left = S.wait.until - now();
        q(".live-wait-text").textContent = S.wait.reason === "all_busy"
          ? `All the free AI services asked for a short break. Argus continues in ${Math.ceil(left / 1000)}s.`
          : S.wait.reason === "busy"
          ? `${PROVIDER[S.wait.provider] || "The AI service"} is busy. Trying again in ${Math.ceil(left / 1000)}s.`
          : `Pausing ${Math.ceil(left / 1000)}s: ${PROVIDER[S.wait.provider] || "the AI service"}'s free tier only allows so much per minute. Argus picks up right after.`;
        q(".live-wait-bar span").style.width = `${Math.max(0, Math.min(100, 100 * left / S.wait.total))}%`;
      }
      // latest actions
      q(".live-actions").innerHTML = S.actions.map((a, i) => `<li class="${a.kind} ${i === 0 && running ? "latest" : ""}">
        <span class="la-when">${esc(fmt(now() - a.ts))} ago</span><span>${esc(a.text)}</span></li>`).join("")
        || `<li class="faint">Steps will appear here as Argus works.</li>`;
      // phase checklist
      const seen = Object.fromEntries(S.phases.map((p) => [p.key, p]));
      const lastKey = cur && cur.key;
      q(".live-phases").innerHTML = ORDER.map((k) => {
        const ph = seen[k];
        const state = !ph ? "todo" : ph.end || (S.done && k !== lastKey) ? "done" : S.done ? (S.done.kind === "ok" ? "done" : "stop") : "now";
        const dur = ph ? fmt((ph.end || (S.done ? S.done.ts : now())) - ph.start) : "";
        return `<li class="${state}"><span class="lp-mark">${state === "done" ? ICON_DONE : ""}</span><span class="lp-label">${esc(LABEL[k])}</span><span class="lp-dur">${esc(dur)}</span></li>`;
      }).join("");
      q(".live-total").textContent = S.started ? `Total so far: ${fmt((S.done ? S.done.ts : now()) - S.started)}` : "";
    }

    setInterval(render, 1000);
    render();
    return { handle(e) { handle(e); }, render };
  }

  return { mount };
})();
