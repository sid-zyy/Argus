// Ticket "war room": a snapshot from GET /incidents/{id}, kept current by the SSE audit-log stream.
const iid = Number(location.pathname.split("/")[2]);
const SNAPSHOT_TYPES = new Set(["attempt", "claim", "verdict", "suspects", "patch", "sandbox", "awaiting_approval",
  "approval_vote", "approval", "qa", "rerun", "escalated", "report", "comment", "updated", "llm_step"]);
const MINOR_TYPES = new Set(["llm_step", "tool_call", "thinking", "waiting"]);
let inc = null;
const diffCache = {};
const openDiffs = new Set();
let firstSuspectOpened = false;

const $ = (id) => document.getElementById(id);
const argus = Argus.mount($("argus"));
const live = LiveProgress.mount($("progress"));
const graph = ImpactGraph.mount($("impact-graph"), iid);
// Events older than this are history (the stream replays the log on connect): set state, skip the animation.
const FRESH_MS = 8000;

// ---- snapshot ----------------------------------------------------------------
let refreshTimer = null;
function scheduleRefresh() {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(refresh, 200);
}

async function refresh() {
  const was = inc && inc.status;
  inc = await api(`/incidents/${iid}`);
  impactLoaded = Date.now();
  render();
  // the fix went through while this page was open: a short cheer from the status badge
  if (was && was !== inc.status && ["resolved", "closed"].includes(inc.status)) {
    const r = $("status").getBoundingClientRect();
    confetti(r.top > 0 && r.top < innerHeight ? { x: r.left + r.width / 2, y: r.top + r.height / 2 } : {});
    toastMsg(inc.status === "closed" ? "Fixed and confirmed by a tester" : "Fix approved and applied", "good");
  }
  renderTabBadges();
  graph.load();
  const key = `${inc.status}|${inc.claims.length}|${inc.patches.length}`;
  if (key !== compareKey) { compareKey = key; loadCompare(); }
}
let compareKey = "";

function render() {
  document.title = `${incKey(inc.id)} · ${inc.title} · Argus`;
  $("crumb").innerHTML = `<span class="crumb-key">${incKey(inc.id)}</span><span class="crumb-src">${esc(SOURCE_LABEL[inc.source] || "Alert")}</span>`
    + (inc.alert && inc.alert.source ? `<span class="crumb-from">from ${esc(inc.alert.source)}</span>` : "");
  $("summary-line").textContent = summaryLine();
  renderStepper();
  $("title").textContent = inc.title;
  $("status").innerHTML = statusBadge(inc.status);
  $("chips").innerHTML = [
    ["Service", inc.service], ["Severity", SEV_LABEL[inc.severity] || inc.severity], ["Assigned to", inc.assignee || "Nobody yet"],
    ["Opened", ago(inc.created_at)], ...(inc.mode === "replay" ? [["Mode", "Replay of a recorded run"]] : []),
  ].map(([k, v]) => `<span class="chip"><span class="faint">${esc(k)}</span> ${esc(v)}</span>`).join("");
  renderAlert();
  renderImpact();
  renderReportBox();
  renderDetails();
  renderRerun();
  renderClaims();
  renderSuspects();
  renderFix();
  renderApproval();
  renderComments();
  $("report").innerHTML = inc.report ? renderMarkdown(inc.report) : `<p class="faint">The report is written after the approval decision (or the escalation).</p>`;
  $("download").hidden = !inc.report;
  $("download").href = `/incidents/${iid}/report?download=true`;
}

const SEV_LABEL = { sev1: "Critical", sev2: "Major", sev3: "Minor", sev4: "Small" };
const FIX_LABEL = { patch: "Code fix", config_change: "Settings change", rollback: "Roll back" };

// Where the incident is, in six plain steps.
function renderStepper() {
  const s = inc.status;
  const hasClaims = inc.claims.length > 0, verified = inc.claims.some((c) => c.verdict === "supported");
  const tested = inc.patches.some((p) => p.sandbox);
  const steps = [["Reported", true], ["Looking into it", hasClaims], ["Double-checking", verified], ["Testing a fix", tested],
    ["Waiting for approval", ["awaiting_qa", "resolved", "closed"].includes(s)], ["Fixed", ["resolved", "closed"].includes(s)]];
  let current = steps.findIndex(([, done]) => !done);
  if (current === -1) current = steps.length;
  const stopped = s === "escalated" || s === "rejected";
  $("stepper").innerHTML = steps.map(([label, done], i) => {
    const cls = i < current ? "done" : i === current ? (stopped ? "stop" : s === "awaiting_approval" ? "wait" : "now") : "";
    return `<div class="step ${cls}"><div class="bar"></div><div class="lbl">${esc(label)}</div></div>`;
  }).join("");
}

function summaryLine() {
  const need = inc.required_approvals.count;
  switch (inc.status) {
    case "open": case "investigating": return "Argus is working on this now. You can watch it below.";
    case "awaiting_approval": return `Argus found the cause and a fix that works in testing. It needs ${need === 2 ? "two approvals (one from a team lead)" : "one approval"} before anything changes.`;
    case "awaiting_qa": return "The fix is in. A tester now checks that the problem is really gone.";
    case "resolved": return "Fixed and approved. The summary report is ready.";
    case "closed": return "Fixed, and a tester confirmed it works.";
    case "rejected": return "The suggested fix was declined. Argus can try again.";
    case "escalated": return "Argus couldn't solve this on its own, so it's been handed to the team.";
    default: return "";
  }
}

function money(v) {
  if (v >= 1e7) return `₹${(v / 1e7).toFixed(2)} Cr`;
  if (v >= 1e5) return `₹${(v / 1e5).toFixed(1)} L`;
  return `₹${Math.round(v).toLocaleString("en-IN")}`;
}

function renderImpact() {
  const m = inc.impact;
  $("impact-wrap").hidden = !m || inc.source === "tester_report";
  if (!m) return;
  const minutes = m.fixed ? m.minutes_so_far : m.minutes_so_far + (Date.now() - impactLoaded) / 60000;
  const ai = inc.ai_usage.cost || 0, aiText = `₹${ai.toFixed(2)}`;
  const tiles = m.fixed ? [
    ["Revenue protected", money(m.saved_value), `Fixed ${Math.max(0, m.manual_minutes - m.minutes_so_far).toFixed(0)} min faster than a typical manual fix`, "good"],
    ["Fixed in", `${m.minutes_so_far.toFixed(0)} min`, `Typical manual fix: ${m.manual_minutes.toFixed(0)} min`],
    ["Engineer time saved", `${m.engineer_hours_saved} h`, m.engineer_cost_saved ? `About ${money(m.engineer_cost_saved)} of people's time` : "People who didn't need to be paged"],
    ["AI cost", aiText, ai ? `${Math.round((m.saved_value + m.engineer_cost_saved) / ai).toLocaleString("en-IN")}× return · ${inc.ai_usage.tokens.toLocaleString("en-IN")} tokens` : "No AI calls"],
  ] : [
    ["Revenue at risk", `${money(m.at_risk_per_minute)}/min`, `${Math.round(m.failure_share * 100)}% of ${m.unit} failing`, "bad"],
    ["Cost so far", money(m.at_risk_per_minute * minutes), `${minutes.toFixed(0)} min since the problem started`, "bad"],
    [`${m.unit[0].toUpperCase()}${m.unit.slice(1)} affected`, `${Math.round(m.affected_per_minute).toLocaleString("en-IN")}/min`, m.service_label],
    ["Typical manual fix", `${m.manual_minutes.toFixed(0)} min`, "Argus aims to beat this"],
  ];
  $("impact").innerHTML = tiles.map(([k, v, sub, kind]) => `<div class="tile ${kind || ""}"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div><div class="s">${esc(sub)}</div></div>`).join("");
  $("impact-assumptions").innerHTML = m.assumptions.map((a) => `<li>${esc(a)}</li>`).join("")
    + ((m.sources || []).length ? `<li>Sources: ${m.sources.map((u) => `<a href="${esc(u)}" target="_blank" rel="noopener">${esc(new URL(u).hostname.replace(/^www\./, ""))}</a>`).join(", ")}</li>` : "");
}
let impactLoaded = Date.now();

function renderAlert() {
  const a = inc.alert;
  const when = (t) => (t ? new Date(t).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "–");
  const rows = [["Problem", a.title], ["Measured", a.metric ? `${a.metric.replace(/_/g, " ")} reached ${a.value} (normal is below ${a.threshold})` : "–"],
    ["Started", when(a.started_at)], ["Alert sent", when(a.fired_at)], ["Alert ID", a.id]];
  $("alert").innerHTML = rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
}

function evidenceList(evs) {
  if (!evs.length) return "";
  return `<ul class="ev">${evs.map((e) => `<li><span class="src">${esc(e.source)}:${e.line}</span><span class="quote">${esc(e.quote)}</span></li>`).join("")}</ul>`;
}

function renderClaims() {
  if (!inc.claims.length) {
    const busy = ["open", "investigating"].includes(inc.status);
    $("claims").innerHTML = `<p class="faint">${busy ? '<span class="pending-dot"></span> Argus is reading the logs. Possible causes will appear here.' : "No possible causes were found."}</p>`;
    return;
  }
  const byAttempt = {};
  inc.claims.forEach((c) => (byAttempt[c.attempt] ||= []).push(c));
  const busy = inc.status === "investigating";
  $("claims").innerHTML = Object.entries(byAttempt).map(([n, claims]) => `
    <div class="attempt-label">${Number(n) > 1 ? `Try ${esc(n)}: after the earlier guesses were ruled out` : "First look"}</div>
    ${claims.map((c) => {
      const v = c.verdict;
      const badge = v ? `<span class="badge v-${esc(v)}">${esc(VERDICT_LABEL[v])}</span>`
        : busy ? `<span class="badge v-pending">Checking…</span>` : `<span class="badge v-insufficient">Not checked</span>`;
      return `<article class="claim ${esc(v || "")}">
        <div class="claim-head"><span class="mono small">${esc(c.claim_key)}</span><span class="chip">${esc(c.category)}</span><span class="spacer"></span>${badge}</div>
        <div class="statement">${esc(c.statement)}</div>
        <div class="ev-title">Why Argus thought so</div>
        ${evidenceList(c.evidence)}
        ${v ? `<div class="verifier"><div class="who">Fact checker: ${esc(VERDICT_LABEL[v].toLowerCase())}</div><div>${esc(c.reasoning)}</div>${c.verdict_evidence.length ? `<div class="ev-title" style="margin-top:8px">Evidence it used</div>` : ""}${evidenceList(c.verdict_evidence)}</div>` : ""}
      </article>`;
    }).join("")}`).join("");
}

function citedLines(sha) {
  const lines = new Set();
  const match = (src) => src.startsWith("git:") && (src.slice(4).startsWith(sha) || sha.startsWith(src.slice(4)));
  const all = [...inc.claims.flatMap((c) => [...c.evidence, ...c.verdict_evidence]), ...inc.suspects.flatMap((s) => s.evidence || [])];
  all.forEach((e) => match(e.source) && lines.add(e.line));
  return lines;
}

function renderSuspects() {
  $("suspects-panel").hidden = !inc.suspects.length;
  if (!inc.suspects.length) return;
  if (!firstSuspectOpened) { openDiffs.add(inc.suspects[0].sha); firstSuspectOpened = true; }
  $("suspects").innerHTML = inc.suspects.map((s) => {
    const open = openDiffs.has(s.sha);
    const diff = diffCache[s.sha];
    return `<div class="suspect">
      <div class="row"><span class="mono">${esc(s.sha)}</span><strong>${esc(s.summary)}</strong><span class="spacer" style="flex:1"></span>
        <button class="btn small" data-sha="${esc(s.sha)}">${open ? "Hide the change" : "Show the change"}</button></div>
      <div class="small muted" style="margin-top: 4px">${esc(s.reason)}</div>
      ${open ? (diff ? `<div class="diff">${diffLines(diff.split("\n"), citedLines(s.sha))}</div>
        <div class="small faint" style="margin-top: 6px">Lines marked in yellow are the ones Argus pointed to as evidence. Red lines were removed, green lines were added.</div>` : `<p class="faint small">Loading…</p>`) : ""}
    </div>`;
  }).join("");
  $("suspects").querySelectorAll("button[data-sha]").forEach((b) => b.onclick = () => {
    const sha = b.dataset.sha;
    openDiffs.has(sha) ? openDiffs.delete(sha) : openDiffs.add(sha);
    renderSuspects();
  });
  openDiffs.forEach((sha) => {
    if (diffCache[sha] === undefined) {
      diffCache[sha] = null;
      fetch(`/incidents/${iid}/commits/${encodeURIComponent(sha)}`).then((r) => r.text()).then((t) => { diffCache[sha] = t; renderSuspects(); });
    }
  });
}

function sandboxBox(label, run) {
  if (!run) return `<div class="sb"><strong>${esc(label)}</strong><p class="faint small">Not tested</p></div>`;
  let body = `<pre class="small">${esc(run.output)}</pre>`;
  try {
    const j = JSON.parse(run.output);
    body = `<dl class="kv" style="margin-top: 6px">${Object.entries(j).map(([k, v]) =>
      `<dt>${esc(k)}</dt><dd>${esc(typeof v === "object" ? JSON.stringify(v) : v)}</dd>`).join("")}</dl>`;
  } catch { /* not JSON: show raw output */ }
  const ok = run.exit_code === 0;
  return `<div class="sb"><div class="row"><strong>${esc(label)}</strong><span class="badge ${ok ? "pass" : "fail"}">${ok ? "Working" : "Broken"}</span>
    <span class="faint small">exit ${run.exit_code} · ${run.seconds}s</span></div>${body}</div>`;
}

function renderFix() {
  const patches = inc.patches;
  $("fix-panel").hidden = !patches.length;
  if (!patches.length) return;
  const p = patches[patches.length - 1];
  const sb = p.sandbox;
  const earlier = patches.length > 1 ? `<p class="note">${patches.length - 1} earlier fix${patches.length > 2 ? "es" : ""} didn't work in testing, so Argus tried again.</p>` : "";
  const result = sb ? `<span class="badge ${sb.passed ? "pass" : "fail"}">${sb.passed ? "Works in testing" : "Didn't work in testing"}</span>` : `<span class="badge v-pending">Testing…</span>`;
  const testsLine = sb && sb.tests ? `<p class="small muted">The project's own tests ${sb.tests.exit_code === 0 ? "all pass" : "fail"} with this fix.</p>` : "";
  $("fix").innerHTML = `${earlier}
    <div class="row" style="flex-wrap:wrap"><span class="chip">${esc(FIX_LABEL[p.fix_type] || p.fix_type)}${p.rollback_to ? ` to ${esc(p.rollback_to)}` : ""}</span>${result}</div>
    <p>${esc(p.rationale)}</p>
    ${p.diff ? `<div class="diff">${diffLines(p.diff.split("\n"))}</div>` : ""}
    ${sb && sb.error ? `<p class="error-text">${esc(sb.error)}</p>` : ""}
    ${sb ? `<div class="sandbox">${sandboxBox("Without the fix", sb.before)}${sandboxBox("With the fix", sb.after)}</div>${testsLine}` : ""}`;
}

function renderApproval() {
  const box = $("approval");
  const u = Session.user;
  const need = inc.required_approvals;
  const yes = inc.votes.filter((v) => v.decision === "approved");
  const voteList = () => {
    const leadVote = yes.find((v) => v.role === "lead");
    const other = yes.find((v) => v !== leadVote);
    const slots = need.count === 2 ? [["Developer or team lead", other], ["Team lead", leadVote]] : [["Developer or team lead", yes[0]]];
    return `<ul class="votes">${slots.map(([slot, v]) => `<li>${v
      ? `${avatar(v.name, v.role)}<b>${esc(v.name)}</b><span class="faint">${esc(ROLE_LABEL[v.role])}</span><span class="badge pass" style="margin-left:auto">Approved</span>`
      : `<span class="avatar empty">?</span><span class="slot">${esc(slot)}</span><span class="badge s-awaiting_approval" style="margin-left:auto">Waiting</span>`}</li>`).join("")}</ul>`;
  };
  if (inc.status === "awaiting_approval") {
    let blocker = "";
    if (!u) blocker = "Choose who you are (top right) to approve or decline this fix.";
    else if (!["developer", "lead"].includes(u.role)) blocker = `Developers and team leads approve fixes. You're signed in as a ${ROLE_LABEL[u.role].toLowerCase()}.`;
    else if (need.count === 2 && inc.reported_by === u.id) blocker = "You reported this problem, so someone else needs to approve the fix. That way two people always look at it.";
    else if (inc.votes.some((v) => v.user_id === u.id)) blocker = "Thanks, your approval is in. Waiting for the second person.";
    else if (need.count === 2 && u.role === "developer" && yes.length === 1 && yes[0].role !== "lead") blocker = "A developer has already approved. The second approval needs to come from a team lead.";
    box.innerHTML = `
      <div id="sla" class="note" style="margin-top:0"></div>
      <p class="small">Argus found the cause and tested a fix. Nothing changes until it's approved.</p>
      <div class="small muted">${need.count === 2 ? "This is a serious issue, so two people must approve, and one must be a team lead." : "One developer or team lead needs to approve."}</div>
      ${voteList()}
      ${blocker ? `<div class="note">${esc(blocker)}</div>` : `
        <label for="ap-reason">Comment <span class="field-hint">needed if you decline</span></label><textarea id="ap-reason" placeholder="e.g. Checked the change, looks safe"></textarea>
        <div class="row" style="margin-top: 10px"><button class="btn primary" id="ap-yes">Approve fix</button><button class="btn danger" id="ap-no">Decline</button></div>`}
      <div id="ap-err" class="error-text"></div>`;
    renderSla();
    if (!blocker) {
      const send = async (approve) => {
        try {
          const r = await api(`/incidents/${iid}/${approve ? "approve" : "reject"}`, { method: "POST", body: { reason: $("ap-reason").value } });
          toastMsg(r.complete ? (r.approved ? "Fix approved. Thank you!" : "Fix declined") : `Approval saved. Still needed: ${r.still_needed}.`, r.complete && !r.approved ? "bad" : "good");
          scheduleRefresh();
        } catch (e) { $("ap-err").textContent = e.message; }
      };
      $("ap-yes").onclick = () => send(true);
      $("ap-no").onclick = () => send(false);
    }
  } else if (inc.status === "awaiting_qa") {
    const canQa = Session.is("tester");
    box.innerHTML = `${voteList()}
      <div class="note">The fix is in (simulated). Please try the steps again and tell Argus whether the problem is gone.</div>
      ${canQa ? `<label for="qa-note">What did you try? <span class="field-hint">optional</span></label><textarea id="qa-note" placeholder="e.g. Paid with a declined card, then a good one"></textarea>
        <div class="row" style="margin-top: 10px"><button class="btn primary" id="qa-yes">Yes, it's fixed</button><button class="btn danger" id="qa-no">No, still broken</button></div>`
        : `<p class="small faint">A tester checks this step. Switch to Tara (top right) to try it.</p>`}
      <div id="ap-err" class="error-text"></div>`;
    if (canQa) {
      const send = async (verified) => {
        try {
          await api(`/incidents/${iid}/qa`, { method: "POST", body: { verified, note: $("qa-note").value } });
          toastMsg(verified ? "Great, marked as fixed" : "Thanks. Argus will take another look", verified ? "good" : "bad");
          scheduleRefresh();
        } catch (e) { $("ap-err").textContent = e.message; }
      };
      $("qa-yes").onclick = () => send(true);
      $("qa-no").onclick = () => send(false);
    }
  } else if (inc.approval) {
    const a = inc.approval;
    box.innerHTML = `<p style="margin-top:0"><span class="badge ${a.approved ? "pass" : "fail"}">${a.approved ? "Approved" : "Declined"}</span>
      by <strong>${esc(a.approver)}</strong></p>
      ${a.reason ? `<p class="small">${esc(a.reason)}</p>` : ""}
      ${inc.qa_checks.map((q) => `<div class="note">QA by ${esc(q.name)}: ${q.result === "verified" ? "verified fixed" : "still failing"}${q.note ? `, ${esc(q.note)}` : ""}</div>`).join("")}
      <p class="small faint">${a.approved ? "This is a demo, so the fix is applied in a pretend system only." : "The fix was not applied."}</p>`;
  } else if (inc.status === "escalated") {
    box.innerHTML = `<p class="small" style="margin-top:0">Argus couldn't get to a tested fix, so there's nothing to approve. Someone on the team should take a look.</p>`;
  } else {
    box.innerHTML = `<p class="small muted" style="margin-top:0">Nothing to decide yet. Once Argus has a tested fix, you'll be able to approve it here.</p>
      <div class="small faint">${need.count === 2 ? "Serious issue: needs two approvals, one from a team lead." : "Needs one approval."}</div>`;
  }
  box.insertAdjacentHTML("beforeend", policyLine());
}

// Which rule of policy.yaml governs this decision. Once decided, the rule recorded with the decision is shown.
function policyLine() {
  const decided = inc.approval && inc.approval.rule ? inc.approval : null;
  const r = decided || inc.required_approvals;
  if (!r.rule) return "";
  return `<p class="policy-line"><a href="/check#policy" title="See the whole policy">Rule <code>${esc(r.rule)}</code></a>
    ${decided ? "applied" : "applies"}, from ${esc(r.policy_file)} <span class="mono" title="Fingerprint of the policy file">${esc(r.policy_sha)}</span></p>`;
}

// ---- Argus vs a single chatbot prompt -------------------------------------------------------------------------
let compareData = null, compareBusy = false;
async function loadCompare() {
  try { compareData = await api(`/incidents/${iid}/compare`); } catch { compareData = null; }
  renderCompare();
}
function renderCompare() {
  const d = compareData;
  const done = inc && inc.claims.some((c) => c.verdict === "supported");
  $("compare-panel").hidden = !d || !done;
  $("compare-run").hidden = !Session.is("developer", "lead") || compareBusy;
  if (!d || !done) return;
  const mark = (v, yes, no) => v == null ? "" : `<li class="${v ? "yes" : "no"}"><span class="mark">${v ? "✓" : "✗"}</span>${esc(v ? yes : no)}</li>`;
  const side = (title, s, kind) => !s ? `<div class="cmp-side"><h3>${title}</h3><p class="small faint">${kind === "chatbot"
      ? "Not asked yet for this incident. A developer can ask one now (it uses one real AI call)." : ""}</p></div>` : `
    <div class="cmp-side"><h3>${title}</h3><p class="cmp-cause">“${esc(s.root_cause)}”</p><ul class="cmp-checks">
      ${mark(s.right_category, "Right kind of cause", "Wrong kind of cause")}
      ${mark(s.named_culprit, "Named the change that caused it", "Didn't name the change that caused it")}
      ${mark(s.evidence_total ? s.evidence_verified === s.evidence_total : null,
        `All ${s.evidence_total} quotes match the real files`, `${s.evidence_total - s.evidence_verified} of ${s.evidence_total} quotes don't match the real files`)}
      ${kind === "argus" ? mark(s.wrong_guess_caught || null, `Ruled out ${s.guesses_ruled_out.length} wrong guess${s.guesses_ruled_out.length === 1 ? "" : "es"} first`, "")
        : mark(false, "", "Nothing checks its guess")}
      ${mark(s.fix_passes, "Fix works on a safe copy", `Fix fails on a safe copy${s.fix_note && s.fix_note !== "passed" ? ` (${s.fix_note})` : ""}`)}
      ${kind === "argus" ? mark(s.approved_by_person, "A person approved it", "Waiting for a person to approve") : ""}
    </ul>${kind === "chatbot" ? `<p class="small faint" style="margin:8px 0 0">${s.source === "run" ? `Asked ${ago(s.ran_at)} by ${esc(s.by)}` : "Recorded run"} · ${esc(s.model)}</p>` : ""}</div>`;
  $("compare").innerHTML = (d.trap ? `<p class="small" style="margin-top:0"><b>The misleading clue in this incident:</b> ${esc(d.trap)}.</p>` : "")
    + `<div class="cmp-grid">${side("Argus", d.argus, "argus")}${side("A single chatbot prompt", d.chatbot, "chatbot")}</div>`;
}
$("compare-run").onclick = async () => {
  compareBusy = true;
  $("compare-run").hidden = true;
  $("compare").insertAdjacentHTML("afterbegin", `<p class="note" id="compare-wait" style="margin-top:0">Asking a chatbot with the paste an engineer would make, then testing its fix…</p>`);
  try { compareData = await api(`/incidents/${iid}/compare`, { method: "POST" }); toastMsg("Chatbot answer scored"); }
  catch (e) { toastMsg(e.message, "bad"); }
  finally { compareBusy = false; renderCompare(); }
};

// ---- tamper-evident audit trail -------------------------------------------------------------------------------
async function verifyAudit() {
  const el = $("audit-state");
  el.className = "audit-state";
  el.textContent = "Checking every event's hash…";
  try {
    const v = await api(`/audit/verify?incident=${iid}`);
    el.className = `audit-state ${v.ok ? "ok" : "bad"}`;
    el.innerHTML = v.ok
      ? `<b>Intact.</b> All ${v.events.toLocaleString()} events (${v.incident_events} for this incident) match their hashes. Chain head <span class="mono">${esc(v.head)}</span>.`
      : `<b>Changed after it was written:</b> event ${v.first_bad.id} of ${incKey(v.first_bad.incident_id)}. ${esc(v.first_bad.problem)}.`;
  } catch (e) { el.textContent = e.message; }
}
$("audit-verify").onclick = verifyAudit;

function renderSla() {
  const el = $("sla");
  if (!el || !inc.approval_waiting_since || !inc.approval_sla_minutes) { if (el) el.hidden = true; return; }
  const left = Date.parse(inc.approval_waiting_since) + inc.approval_sla_minutes * 60000 - Date.now();
  el.className = `note ${left <= 0 || inc.sla_breached ? "warn" : ""}`;
  el.textContent = left > 0 ? `Please decide within ${Math.floor(left / 60000)}:${String(Math.floor(left / 1000) % 60).padStart(2, "0")}. After that the team lead is alerted.`
    : "Overdue. The team lead has been alerted.";
}
setInterval(() => { if (inc && inc.status === "awaiting_approval") renderSla(); if (inc && inc.impact && !inc.impact.fixed) renderImpact(); }, 1000);

function renderRerun() {
  const box = $("rerun-box");
  const can = ["escalated", "rejected"].includes(inc.status) && inc.mode === "live";
  $("escalation-panel").hidden = !["escalated", "rejected"].includes(inc.status);
  $("escalation-title").textContent = inc.status === "rejected" ? "The fix was declined" : "Argus needs a person";
  $("escalation").textContent = inc.status === "rejected" ? "Someone declined the suggested fix. Argus can try again with your guidance." : `Argus couldn't solve this one on its own. ${inc.escalation || ""}`;
  if (!can) { box.innerHTML = ""; return; }
  box.innerHTML = Session.is("developer", "lead")
    ? `<label for="rr-note">Any hints for Argus? <span class="field-hint">optional</span></label><textarea id="rr-note" placeholder="e.g. look at the settings that changed in the last deploy"></textarea>
       <div class="row" style="margin-top:10px"><button class="btn primary" id="rr-go">Ask Argus to try again</button></div>`
    : `<p class="small faint">A developer or team lead can ask Argus to try again.</p>`;
  const go = $("rr-go");
  if (go) go.onclick = async () => {
    try { await api(`/incidents/${iid}/rerun`, { method: "POST", body: { note: $("rr-note").value } }); toastMsg("Argus is trying again"); scheduleRefresh(); }
    catch (e) { toastMsg(e.message, "bad"); }
  };
}

function renderDetails() {
  const canEdit = Session.is("developer", "lead");
  const sel = $("assignee");
  if (document.activeElement !== sel) {
    sel.innerHTML = `<option value="0">Unassigned</option>` + Session.users.filter((x) => x.role !== "tester")
      .map((x) => `<option value="${x.id}" ${inc.assignee_id === x.id ? "selected" : ""}>${esc(x.name)} (${esc(ROLE_LABEL[x.role])})</option>`).join("");
  }
  $("severity").value = inc.severity;
  sel.disabled = $("severity").disabled = !canEdit;
  $("details-hint").textContent = canEdit ? "" : "Developers and team leads can change these.";
  $("comment-label").textContent = Session.user ? `Add a note as ${Session.user.name}` : "Choose who you are (top right) to add notes";
  $("post-comment").disabled = !Session.user;
}

function renderReportBox() {
  const isBug = inc.source === "tester_report";
  $("alert-title").textContent = isBug ? `Reported by ${inc.reported_by_name || "a tester"}` : "What happened";
  $("alert").hidden = isBug;
  const box = $("bug-report");
  box.hidden = !isBug;
  if (isBug) {
    box.innerHTML = [["What they did", inc.repro_steps], ["What they expected", inc.expected], ["What happened instead", inc.actual],
      ["Where", inc.environment], ["Error message", inc.log_snippet]].filter(([, v]) => v)
      .map(([k, v]) => `<dt>${esc(k)}</dt><dd${k === "Error message" ? ' class="mono"' : ""}>${esc(v)}</dd>`).join("");
  }
}

function renderComments() {
  $("comments").innerHTML = inc.comments.length ? inc.comments.map((c) =>
    `<div class="comment"><div class="small"><strong>${esc(c.author)}</strong> <span class="faint">${clock(c.ts)}</span></div><div>${esc(c.text)}</div></div>`).join("")
    : `<p class="small faint">No notes yet.</p>`;
}

// ---- activity log ------------------------------------------------------------
function addActivity(e) {
  const el = document.createElement("div");
  el.className = "act" + (MINOR_TYPES.has(e.type) ? " minor" : "");
  el.dataset.minor = MINOR_TYPES.has(e.type) ? "1" : "";
  el.hidden = MINOR_TYPES.has(e.type) && !$("show-steps").checked;
  el.innerHTML = `<span class="t">${esc(clock(e.ts))}</span><div><span class="actor">${esc(actorName(e))}</span> <span class="detail">${esc(describeEvent(e))}</span></div>`;
  $("tb-timeline").textContent = $("activity").querySelectorAll(".act:not(.minor)").length + (MINOR_TYPES.has(e.type) ? 0 : 1) || "";
  const box = $("activity");
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  box.appendChild(el);
  if (atBottom) box.scrollTop = box.scrollHeight;
}

function connect() {
  const es = new EventSource(`/incidents/${iid}/events`);
  es.onopen = () => { $("live").textContent = "● Live"; };
  es.onerror = () => { $("live").textContent = "Reconnecting…"; };
  es.onmessage = (m) => {
    const e = JSON.parse(m.data);
    addActivity(e);
    argus.handle(e, Date.now() - Date.parse(e.ts) < FRESH_MS);
    live.handle(e);
    live.render();
    if (SNAPSHOT_TYPES.has(e.type)) scheduleRefresh();
    if (e.type === "question") showQuestion(e);
  };
}

// ---- Ask Argus -----------------------------------------------------------------
const ASK_IDEAS = ["Why did it break?", "Who made the change?", "Is the fix safe to approve?", "What do I tell customers?", "What did this cost us?"];
const asked = new Set();  // question event ids already on screen
const pending = new Set();  // questions this page is waiting on, so the stream doesn't show them twice
const cite = (html) => html.replace(/\[([^\[\]<>]{1,60})\]/g, '<span class="cite">$1</span>');
function askBubble(q, by) {
  const el = document.createElement("div");
  el.className = "ask-q";
  el.textContent = q;
  if (by) el.title = `Asked by ${by}`;
  $("ask-empty").hidden = true;
  $("ask-log").appendChild(el);
  askCount();
  return el;
}
// how many questions this incident has, on the floating button and under the ask bar
function askCount() {
  const n = $("ask-log").querySelectorAll(".ask-q").length;
  $("ask-fab-n").textContent = n || "";
  $("ask-open").hidden = !n;
  $("ask-open").textContent = `Open the conversation (${n})`;
}
function answerBubble() {
  const el = document.createElement("div");
  el.className = "ask-a";
  el.innerHTML = `<img src="/static/logo.svg" alt=""><div><div class="bubble"><span class="ask-typing"><span></span><span></span><span></span></span></div><div class="meta"></div></div>`;
  $("ask-log").appendChild(el);
  $("ask-log").scrollTop = $("ask-log").scrollHeight;
  return el;
}
function fillAnswer(el, r, by) {
  el.querySelector(".bubble").innerHTML = cite(renderMarkdown(r.answer));
  el.querySelector(".meta").textContent = [by && `Asked by ${by}`, r.provider && `answered by ${r.provider}`].filter(Boolean).join(" · ");
  $("ask-log").scrollTop = $("ask-log").scrollHeight;
}
function showQuestion(e) {  // from the audit log: earlier questions, or ones teammates ask while you watch
  if (asked.has(e.id)) return;
  asked.add(e.id);
  if (pending.has(e.payload.question) && Session.user && e.payload.by === Session.user.name) return;
  askBubble(e.payload.question, e.payload.by);
  fillAnswer(answerBubble(), e.payload, e.payload.by);
}
async function ask(question) {
  question = question.trim();
  if (!question) return;
  if (!Session.user) return toastMsg("Choose who you are first (top right)", "bad");
  openAsk();
  $("ask-input").value = $("ask-bar-input").value = "";
  $("ask-btn").disabled = $("ask-bar-btn").disabled = true;
  askBubble(question);
  const bubble = answerBubble();
  pending.add(question);
  try {
    const r = await api(`/incidents/${iid}/ask`, { method: "POST", body: { question } });
    asked.add(r.id);
    fillAnswer(bubble, r);
  } catch (err) {
    bubble.querySelector(".bubble").textContent = `I couldn't answer that: ${err.message}`;
  } finally { pending.delete(question); $("ask-btn").disabled = $("ask-bar-btn").disabled = false; $("ask-input").focus(); }
}
// The conversation lives in a drawer that opens over any tab: from the ask bar, the floating button, "/" or the graph.
function openAsk(prefill) {
  $("ask-drawer").classList.add("open");
  $("ask-drawer").setAttribute("aria-hidden", "false");
  $("ask-fab").hidden = true;
  if (prefill) $("ask-input").value = prefill;
  $("ask-log").scrollTop = $("ask-log").scrollHeight;
  setTimeout(() => $("ask-input").focus(), 50);
}
function closeAsk() {
  $("ask-drawer").classList.remove("open");
  $("ask-drawer").setAttribute("aria-hidden", "true");
  $("ask-fab").hidden = false;
}
window.openAsk = openAsk;  // the impact graph's "Ask Argus" buttons
const chips = ASK_IDEAS.map((q) => `<button type="button">${esc(q)}</button>`).join("");
$("ask-chips").innerHTML = $("ask-bar-chips").innerHTML = chips;
document.querySelectorAll("#ask-chips button, #ask-bar-chips button").forEach((b) => b.onclick = () => ask(b.textContent));
$("ask-form").onsubmit = (e) => { e.preventDefault(); ask($("ask-input").value); };
$("ask-bar").onsubmit = (e) => { e.preventDefault(); ask($("ask-bar-input").value); };
$("ask-fab").onclick = () => openAsk();
// the floating button only shows once the ask bar has scrolled away
new IntersectionObserver(([en]) => $("ask-fab").classList.toggle("away", en.isIntersecting)).observe($("ask-bar"));
$("ask-open").onclick = () => openAsk();
$("ask-close").onclick = closeAsk;
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && $("ask-drawer").classList.contains("open")) return closeAsk();
  if (e.key !== "/" || (e.target.closest && e.target.closest("input, textarea, select")) || e.metaKey || e.ctrlKey || e.altKey) return;
  e.preventDefault();
  openAsk();
});

// ---- controls ----------------------------------------------------------------
// ---- tabs: Live, Impact graph, Findings, Timeline, Report (the decision column stays beside all but the graph) ----
const TABS = [...document.querySelectorAll("#inc-tabs [data-tab]")].map((b) => b.dataset.tab);
function showTab(name, push = true) {
  if (!TABS.includes(name)) name = "live";
  document.querySelectorAll("#inc-tabs [data-tab]").forEach((b) => {
    b.classList.toggle("on", b.dataset.tab === name);
    b.setAttribute("aria-selected", b.dataset.tab === name);
  });
  document.querySelectorAll("[data-pane]").forEach((el) => (el.hidden = el.dataset.pane !== name));
  $("main-grid").hidden = name === "impact";
  document.querySelector("main.wrap").classList.toggle("wide", name === "impact");  // the graph gets the whole screen width
  if (push) history.replaceState(null, "", `#${name}`);
  store.set("ic.tab", name);
  if (name === "impact") graph.show();
  if (name === "timeline" && typeof verifyAudit === "function" && inc) verifyAudit();
  dispatchEvent(new Event("resize"));  // the sphere and the graph size themselves to what is visible
}
document.querySelectorAll("#inc-tabs [data-tab]").forEach((b) => b.onclick = () => showTab(b.dataset.tab));
document.addEventListener("keydown", (e) => {
  if ((e.target.closest && e.target.closest("input, textarea, select")) || e.metaKey || e.ctrlKey || e.altKey) return;
  const i = Number(e.key) - 1;
  if (i >= 0 && i < TABS.length) showTab(TABS[i]);
});
addEventListener("hashchange", () => showTab(location.hash.slice(1), false));
showTab(location.hash.slice(1) || "live", false);
window.showTab = showTab;  // the impact graph's buttons jump to the decision and the report

function renderTabBadges() {
  const live = ["open", "investigating"].includes(inc.status);
  $("tb-live").className = `tb-badge ${live ? "pulse" : ""}`;
  $("tb-live").textContent = live ? "" : "";
  const failing = inc.claims.filter((c) => c.verdict === "supported").length;
  $("tb-findings").textContent = inc.claims.length ? `${failing}/${inc.claims.length}` : "";
  $("tb-findings").title = "Confirmed causes / possible causes";
  $("tb-report").textContent = inc.report ? "Ready" : "";
  $("tb-report").className = `tb-badge ${inc.report ? "ok" : ""}`;
}
$("show-steps").checked = store.get("ic.steps", "0") === "1";
$("show-steps").onchange = () => {
  store.set("ic.steps", $("show-steps").checked ? "1" : "0");
  document.querySelectorAll(".act").forEach((el) => { if (el.dataset.minor) el.hidden = !$("show-steps").checked; });
};
const patch = (body) => api(`/incidents/${iid}`, { method: "PATCH", body }).then(scheduleRefresh).catch((e) => toastMsg(e.message, "bad"));
$("assignee").onchange = () => patch({ assignee_id: Number($("assignee").value) });
$("severity").onchange = () => patch({ severity: $("severity").value });
$("post-comment").onclick = async () => {
  const text = $("comment-text").value.trim();
  if (!text) return;
  try { await api(`/incidents/${iid}/comment`, { method: "POST", body: { text } }); $("comment-text").value = ""; }
  catch (e) { toastMsg(e.message, "bad"); }
};

mountUserMenu($("user-menu"));
mountThemeToggle($("theme-btn"));
Session.onChange(() => inc && render());
Session.load().then(refresh).then(connect).catch((e) => { $("title").textContent = e.message; });
setInterval(() => inc && ["open", "investigating", "awaiting_approval"].includes(inc.status) && render(), 30000);
