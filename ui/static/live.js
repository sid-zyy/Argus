// Live progress panel: what Argus is doing right now and what it plans to do next, so a run never looks frozen.
// Fed with every audit-log event (history first, then live); a 1 s ticker keeps timers and countdowns moving.
const LiveProgress = (() => {
  const PROVIDER = { groq: "Groq", gemini: "Gemini", openrouter: "OpenRouter", ollama: "Local model", scripted: "Test model" };
  // which result event completes a plan step for each agent
  const PLAN_AGENT = {
    investigator: ["triage", "claim", "Theia"], fact_checker: ["verifier", "verdict", "Themis"],
    change_finder: ["code_history", "suspects", "Chronos"], fixer: ["fix", "patch", "Hephaestus"], test_lab: ["sandbox", "sandbox", "Talos"],
  };
  const fmt = (ms) => {
    const s = Math.max(0, Math.round(ms / 1000));
    return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
  };
  const kfmt = (n) => (n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}K` : String(n));

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
      <div class="live-side"><div class="live-side-title">Plan</div><p class="plan-why" hidden></p><ol class="plan"></ol></div>
      <div class="usage" style="grid-column: 1 / -1"></div>`;
    const q = (s) => el.querySelector(s);
    const S = { phases: [], agent: null, step: 0, max: 0, stepStart: null, thought: "", wait: null, actions: [], done: null,
      started: null, plan: null, results: {}, sources: { logs: new Set(), metrics: false, commits: new Set(), files: new Set() },
      tokens: 0, calls: 0 };
    const now = () => Date.now();
    const t = (e) => Date.parse(e.ts);

    function addAction(who, text, kind = "", ts = now()) {
      S.actions.unshift({ who, text, kind, ts });
      S.actions.length = Math.min(S.actions.length, 4);
    }

    function handle(e) {
      const p = e.payload || {};
      const who = AGENT_NAME[e.actor] || e.actor;
      if (["claim", "verdict", "suspects", "patch", "sandbox"].includes(e.type)) S.results[e.type] = (S.results[e.type] || 0) + 1;
      switch (e.type) {
        case "incident_opened":
          Object.assign(S, { started: t(e), done: null, phases: [], actions: [], agent: null, thought: "", wait: null, plan: null, results: {} });
          break;
        case "rerun":
          S.done = null; S.wait = null;
          addAction(p.by, "Asked Argus to try again", "", t(e));
          break;
        case "phase": {
          const last = S.phases[S.phases.length - 1];
          if (last && !last.end) last.end = t(e);
          S.phases.push({ key: p.key, label: p.label, detail: p.detail, start: t(e), attempt: p.attempt });
          S.agent = null; S.thought = ""; S.wait = null;
          break;
        }
        case "plan":
          S.plan = { ...p, base: { ...S.results } };
          addAction("Commander", p.version > 1 ? "Made a new plan" : "Made a plan", "key", t(e));
          break;
        case "thinking":
          S.agent = e.actor; S.step = p.step; S.max = p.max_steps; S.stepStart = t(e);
          break;
        case "llm_step":
          S.wait = null; S.calls += 1; S.tokens += p.tokens || 0;
          if (p.thought) S.thought = p.thought;
          if (p.provider) S.provider = p.provider;
          break;
        case "waiting":
          S.wait = { until: t(e) + p.seconds * 1000, total: p.seconds * 1000, provider: p.provider, reason: p.reason };
          break;
        case "failover":
          addAction("Argus", `${PROVIDER[p.provider] || p.provider} ${p.reason}. Switched to another AI service`, "warn", t(e));
          break;
        case "tool_call": {
          const a = p.args || {};
          if (p.tool === "search_logs" || p.tool === "read_lines") (a.source ? [a.source] : ["all logs"]).forEach((s) => S.sources.logs.add(s));
          if (p.tool === "get_metrics") S.sources.metrics = true;
          if (p.tool === "git_show") S.sources.commits.add(a.sha);
          if (p.tool === "read_file") S.sources.files.add(a.path);
          addAction(who, describeEvent(e), "", t(e));
          break;
        }
        case "rejected": case "claim": case "verdict": case "suspects": case "patch": case "sandbox": {
          const bad = e.type === "rejected" || (e.type === "verdict" && p.verdict !== "supported") || (e.type === "sandbox" && !p.passed);
          const good = (e.type === "verdict" && p.verdict === "supported") || (e.type === "sandbox" && p.passed);
          addAction(who, describeEvent(e), bad ? "bad" : good ? "good" : "key", t(e));
          break;
        }
        case "awaiting_approval":
          S.agent = null; S.wait = null;
          break;
        case "deployed":
          addAction("Argus", `Deployed the fix to ${p.app} (${p.commit})`, "good", t(e));
          break;
        case "approval":
          addAction(p.approver, p.approved ? "Approved the fix" : "Declined the fix", p.approved ? "good" : "bad", t(e));
          break;
        case "report": case "escalated":
          if (S.phases.length && !S.phases[S.phases.length - 1].end) S.phases[S.phases.length - 1].end = t(e);
          S.done = { ts: t(e), kind: e.type === "report" ? "ok" : "stop", reason: p.reason };
          S.agent = null; S.wait = null;
          break;
      }
    }

    function planState() {
      if (!S.plan) return [];
      const used = {};
      let current = -1;
      return S.plan.steps.map((st, i) => {
        const [, kind] = PLAN_AGENT[st.agent] || [];
        const have = (S.results[kind] || 0) - (S.plan.base[kind] || 0);
        used[kind] = used[kind] || 0;
        let state = "todo";
        if (kind && have > used[kind]) { used[kind] += 1; state = "done"; }
        else if (current === -1) { current = i; state = S.done ? "todo" : "now"; }
        return { ...st, state };
      });
    }

    function render() {
      const cur = S.phases[S.phases.length - 1];
      const running = !S.done && cur && cur.key !== "approval";
      el.classList.toggle("is-running", !!running);
      el.classList.toggle("is-waiting", !!S.wait && S.wait.until > now());
      if (S.done) {
        q(".live-phase").textContent = S.done.kind === "ok" ? "All done" : "Stopped: needs a person";
        q(".live-detail").textContent = S.done.kind === "ok" ? "The summary is in the Report tab." : S.done.reason || "";
        q(".live-timer").textContent = S.started ? `Took ${fmt(S.done.ts - S.started)}` : "";
      } else if (cur) {
        q(".live-phase").textContent = cur.attempt > 1 && cur.key !== "plan" ? `${cur.label} (try ${cur.attempt})` : cur.label;
        q(".live-detail").textContent = cur.detail;
        q(".live-timer").textContent = fmt(now() - cur.start);
      } else {
        q(".live-phase").textContent = S.started ? "Getting started" : "Waiting to start";
        q(".live-detail").textContent = "";
        q(".live-timer").textContent = "";
      }
      const ag = q(".live-agent");
      ag.hidden = !S.agent || !!S.done;
      if (S.agent && !S.done) {
        const c = (Argus.AGENTS[S.agent] || {})[document.documentElement.dataset.theme === "dark" ? "dark" : "light"] || [80, 72, 216];
        ag.style.setProperty("--c", c.join(","));
        ag.innerHTML = `<span class="la-dot"></span><b>${esc(AGENT_NAME[S.agent] || S.agent)}</b>
          <span>Step ${S.step} of ${S.max}${S.provider ? ` · ${esc(PROVIDER[S.provider] || S.provider)}` : ""}</span>
          <span class="la-time">${S.stepStart ? fmt(now() - S.stepStart) : ""}</span>`;
      }
      const th = q(".live-thought");
      const thought = S.thought.replace(/\s+/g, " ").trim();
      th.hidden = !thought || !!S.done;
      th.textContent = thought.length > 220 ? "…" + thought.slice(-220) : thought;
      const w = q(".live-wait");
      const waiting = S.wait && S.wait.until > now();
      w.hidden = !waiting;
      if (waiting) {
        const left = Math.ceil((S.wait.until - now()) / 1000);
        q(".live-wait-text").textContent = S.wait.reason === "all_busy" ? `All AI services asked for a short break. Back in ${left}s.`
          : S.wait.reason === "busy" ? `${PROVIDER[S.wait.provider] || "The AI service"} is busy. Retrying in ${left}s.`
          : `Free-tier limit reached. Continuing in ${left}s.`;
        q(".live-wait-bar span").style.width = `${Math.max(0, Math.min(100, 100 * (S.wait.until - now()) / S.wait.total))}%`;
      }
      q(".live-actions").innerHTML = S.actions.map((a, i) => `<li class="${a.kind} ${i === 0 && running ? "latest" : ""}">
        <span class="la-when">${esc(fmt(now() - a.ts))}</span><span><b class="la-who">${esc(a.who)}</b> ${esc(a.text)}</span></li>`).join("");
      // plan
      const steps = planState();
      const why = q(".plan-why");
      why.hidden = !S.plan || S.plan.version < 2;
      if (S.plan && S.plan.version > 1) why.innerHTML = `<b>New plan.</b> ${esc(S.plan.rationale)}`;
      q(".plan").innerHTML = steps.length ? steps.map((s) => `<li class="${s.state}"><span><span class="who">${esc((PLAN_AGENT[s.agent] || [])[2] || s.agent)}</span>${esc(s.goal)}</span></li>`).join("")
        : `<li class="todo"><span>The plan appears here once Argus starts.</span></li>`;
      // what Argus read, and what it cost
      const src = S.sources;
      const read = [src.logs.size && `${src.logs.size} log${src.logs.size > 1 ? "s" : ""}`, src.metrics && "Metrics",
        src.commits.size && `${src.commits.size} commit${src.commits.size > 1 ? "s" : ""}`, src.files.size && `${src.files.size} code file${src.files.size > 1 ? "s" : ""}`].filter(Boolean);
      const total = S.started ? fmt((S.done ? S.done.ts : now()) - S.started) : "";
      q(".usage").innerHTML = [read.length ? `<span>Read: <b>${esc(read.join(" · "))}</b></span>` : "",
        S.calls ? `<span>AI: <b>${S.calls} calls · ${kfmt(S.tokens)} tokens · ≈${(S.tokens / 1000 * 0.3).toFixed(1)} Wh</b></span>` : "",
        total ? `<span>Total: <b>${total}</b></span>` : ""].join("");
    }

    setInterval(render, 1000);
    render();
    return { handle, render };
  }

  return { mount };
})();
