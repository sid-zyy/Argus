// Shared helpers for the tracker pages. All dynamic text goes through esc() or textContent.
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const text = await res.text();
  let data;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) throw new Error((data && data.detail) || `${res.status} ${res.statusText}`);
  return data;
}

const STATUS_LABEL = {
  open: "New", investigating: "Investigating", awaiting_approval: "Needs approval",
  awaiting_qa: "Needs testing", resolved: "Fixed", closed: "Fixed and confirmed", rejected: "Fix declined", escalated: "Needs a person",
};
const VERDICT_LABEL = { supported: "Confirmed", refuted: "Ruled out", insufficient: "Not enough proof" };
// each AI in Argus has a name and keeps its role
const AGENT_ROLE = { triage: "Investigator", code_history: "Change finder", verifier: "Fact checker", fix: "Fixer",
  sandbox: "Test lab", reporter: "Writer", planner: "Commander" };
const AGENT_PERSONA = { triage: "Theia", code_history: "Chronos", verifier: "Themis", fix: "Hephaestus",
  sandbox: "Talos", reporter: "Clio", planner: "Athena" };
const AGENT_NAME = { ...Object.fromEntries(Object.keys(AGENT_ROLE).map((k) => [k, `${AGENT_PERSONA[k]} (${AGENT_ROLE[k]})`])),
  orchestrator: "Argus", human: "Person" };
const statusBadge = (s) => `<span class="badge s-${esc(s)}">${esc(STATUS_LABEL[s] || s)}</span>`;
const incKey = (id) => `INC-${id}`;

function ago(iso) {
  if (!iso) return "";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return "Just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

function clock(iso) {
  const d = new Date(iso);
  return isNaN(d) ? "" : d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

const store = {
  get(k, d) { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* storage unavailable */ } },
};

// Minimal, escape-first markdown for the incident report: headings, lists, code fences, quotes, bold, inline code.
function renderMarkdown(md) {
  const lines = String(md || "").replace(/\r/g, "").split("\n");
  const out = [];
  let inList = 0, inCode = false, code = [];
  const inline = (s) => esc(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[\s(])_([^_]+)_(?=[\s).,]|$)/g, "$1<em>$2</em>");
  const closeList = () => { while (inList > 0) { out.push("</ul>"); inList--; } };
  for (const line of lines) {
    if (line.startsWith("```")) {
      if (inCode) { out.push(`<div class="diff">${diffLines(code)}</div>`); code = []; inCode = false; }
      else { closeList(); inCode = true; }
      continue;
    }
    if (inCode) { code.push(line); continue; }
    if (line.startsWith("|")) {  // simple tables: skip the |---| separator and an empty header row
      closeList();
      const cells = line.replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
      if (cells.every((c) => /^-*$/.test(c))) continue;
      if (out[out.length - 1] !== "</table>") out.push("<table class=\"md\">"); else out.pop();
      out.push(`<tr>${cells.map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`, "</table>");
      continue;
    }
    const h = line.match(/^(#{1,3})\s+(.*)/);
    const li = line.match(/^(\s*)-\s+(.*)/);
    if (h) { closeList(); out.push(`<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`); }
    else if (li) {
      const depth = Math.floor(li[1].length / 2) + 1;
      while (inList < depth) { out.push("<ul>"); inList++; }
      while (inList > depth) { out.push("</ul>"); inList--; }
      out.push(`<li>${inline(li[2])}</li>`);
    } else if (line.startsWith("> ")) { closeList(); out.push(`<blockquote>${inline(line.slice(2))}</blockquote>`); }
    else if (line.trim() === "") { closeList(); }
    else { closeList(); out.push(`<p>${inline(line)}</p>`); }
  }
  closeList();
  if (inCode) out.push(`<div class="diff">${diffLines(code)}</div>`);
  return out.join("\n");
}

// Colour a unified diff. `cited` is a Set of 1-based line numbers to highlight (lines cited as evidence).
function diffLines(lines, cited = new Set()) {
  return lines.map((l, i) => {
    let cls = "";
    if (l.startsWith("+") && !l.startsWith("+++")) cls = "add";
    else if (l.startsWith("-") && !l.startsWith("---")) cls = "del";
    else if (l.startsWith("@@")) cls = "hunk";
    if (cited.has(i + 1)) cls += " cited";
    return `<div class="${cls.trim()}">${esc(l) || " "}</div>`;
  }).join("");
}

// ---- session: demo user picker in the top bar ------------------------------------
const ROLE_LABEL = { tester: "Tester", developer: "Developer", lead: "Team lead" };
const ROLE_HELP = {
  tester: "Reports problems and checks that fixes work",
  developer: "Reviews and approves fixes",
  lead: "Gives the final OK on serious fixes",
};
const SOURCE_LABEL = { alert: "Alert", tester_report: "Bug report" };
const Session = {
  user: null,
  users: [],
  listeners: [],
  async load() {
    [this.user, this.users] = await Promise.all([api("/session/me"), api("/users")]);
    this.listeners.forEach((fn) => fn(this.user));
    return this.user;
  },
  async signIn(id) {
    this.user = await api("/session", { method: "POST", body: { user_id: id } });
    this.listeners.forEach((fn) => fn(this.user));
  },
  onChange(fn) { this.listeners.push(fn); },
  is(...roles) { return !!this.user && roles.includes(this.user.role); },
};

const initials = (name) => String(name || "?").slice(0, 1).toUpperCase();
const avatar = (name, role) => `<span class="avatar r-${esc(role)}">${esc(initials(name))}</span>`;

function mountUserMenu(el) {
  const render = () => {
    const u = Session.user;
    el.innerHTML = `
      <button class="user-btn ${u ? "" : "empty"}" type="button" aria-haspopup="menu">
        ${u ? `${avatar(u.name, u.role)}<span class="user-name">${esc(u.name)}</span><span class="user-role">${esc(ROLE_LABEL[u.role])}</span>`
            : `<span class="user-name">Choose who you are</span>`}
        <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><path d="m6 9 6 6 6-6"/></svg>
      </button>
      <div class="user-pop" role="menu" hidden>
        <div class="user-pop-head">Switch person (demo accounts, no password needed)</div>
        ${Session.users.map((x) => `<button type="button" role="menuitem" data-id="${x.id}" class="${u && u.id === x.id ? "on" : ""}">
          ${avatar(x.name, x.role)}<span class="who"><span>${esc(x.name)} · ${esc(ROLE_LABEL[x.role])}</span><small>${esc(ROLE_HELP[x.role])}</small></span></button>`).join("")}
      </div>`;
    const pop = el.querySelector(".user-pop");
    el.querySelector(".user-btn").onclick = (e) => { e.stopPropagation(); pop.hidden = !pop.hidden; };
    pop.querySelectorAll("button[data-id]").forEach((b) => b.onclick = async () => {
      pop.hidden = true;
      await Session.signIn(Number(b.dataset.id));
      toastMsg(`You're now ${Session.user.name} (${ROLE_LABEL[Session.user.role]})`);
    });
  };
  document.addEventListener("click", () => { const p = el.querySelector(".user-pop"); if (p) p.hidden = true; });
  Session.onChange(render);
  render();
}

function toastMsg(text, kind = "") {
  let box = document.getElementById("page-toast");
  if (!box) { box = document.createElement("div"); box.id = "page-toast"; box.setAttribute("role", "status"); document.body.appendChild(box); }
  box.textContent = text;
  box.className = `show ${kind}`;
  clearTimeout(toastMsg.t);
  toastMsg.t = setTimeout(() => { box.className = ""; }, 3200);
}

// One-line, plain-language description of an audit-log event (activity feeds, board cards).
const TOOL_TEXT = {
  search_logs: (a) => `Searched ${a.source || "the logs"} for "${a.pattern}"`,
  read_lines: (a) => `Read lines ${a.start_line}-${a.end_line} of ${a.source}`,
  list_sources: () => "Checked which logs are available",
  get_metrics: () => "Looked at the health metrics",
  git_log: () => "Looked at recent code changes",
  git_show: (a) => `Read change ${a.sha}`,
  read_file: (a) => `Read ${a.path}`,
  list_files: () => "Looked through the code",
};
const FIX_WORD = { rollback: "rollback", config_change: "settings change", patch: "code fix" };
// Every description is a sentence that starts with a capital letter; the actor is shown separately.
function describeEvent(e) {
  const p = e.payload || {};
  switch (e.type) {
    case "incident_opened": return `Reported: ${p.alert.title}`;
    case "attempt": return p.n > 1 ? `Trying again (attempt ${p.n} of ${p.max})` : p.stage === "fix" ? "Started on a fix" : "Started investigating";
    case "llm_step": return `Thinking (step ${p.step})`;
    case "tool_call": return (TOOL_TEXT[p.tool] || (() => `Used ${p.tool}`))(p.args || {});
    case "rejected": return (p.errors || []).some((x) => /not found|out of range|unknown source/.test(x))
      ? "Quoted evidence that doesn't exist. Sent back to try again" : "Answer didn't pass the checks. Sent back";
    case "claim": return `Possible cause: ${p.statement}`;
    case "verdict": return `${VERDICT_LABEL[p.verdict]}: ${p.claim_id}`;
    case "suspects": return p.suspects[0] ? `Suspicious change: ${p.suspects[0].summary}` : "No suspicious changes found";
    case "patch": return `Suggested a ${FIX_WORD[p.fix_type] || "fix"}`;
    case "sandbox": return p.passed ? "Tested the fix: it works" : "Tested the fix: it didn't work";
    case "awaiting_approval": return "Tested fix is waiting for approval";
    case "approval_vote": return `${p.approver} ${p.decision} the fix${p.reason ? `: ${p.reason}` : ""}`;
    case "approval": return `Fix ${p.approved ? "approved" : "declined"} by ${p.approver}`;
    case "qa": return `${p.tester} ${p.result === "verified" ? "confirmed it's fixed" : "says it's still broken"}${p.note ? `: ${p.note}` : ""}`;
    case "rerun": return `${p.by} asked Argus to try again${p.note ? `: ${p.note}` : ""}`;
    case "escalated": return `Handed to a person: ${p.reason}`;
    case "report": return "Wrote the summary";
    case "comment": return p.text;
    case "updated": return `Updated ${Object.entries(p).filter(([k]) => k !== "by").map(([k, v]) => `${k} to ${v ?? "none"}`).join(", ")}`;
    case "error": return `Something went wrong: ${p.message}`;
    case "thinking": return `Thinking (step ${p.step} of ${p.max_steps})`;
    case "waiting": return p.reason === "busy" ? `Waiting ${p.seconds}s for a busy AI service` : `Pausing ${Math.round(p.seconds)}s for the free AI service's limit`;
    case "failover": return `Switched AI service (${p.provider} ${p.reason})`;
    case "phase": return p.label;
    case "plan": return p.version > 1 ? "Made a new plan" : "Made a plan";
    case "assigned": return `Assigned to ${p.to} (${p.reason.charAt(0).toLowerCase() + p.reason.slice(1)})`;
    case "archived": return `Cleared from the board by ${p.by}`;
    case "deployed": return `Deployed the approved fix to ${p.app} as commit ${p.commit}`;
    case "notified": return `Messaged ${p.to} on ${p.channel}: ${p.message}`;
    case "sla_breach": return `Approval waited over ${p.minutes} min. Escalated to ${p.escalated_to}`;
    default: return JSON.stringify(p).slice(0, 160);
  }
}
const actorName = (e) => (e.actor === "human" ? (e.payload.by || e.payload.approver || e.payload.author || e.payload.tester || "Person") : AGENT_NAME[e.actor] || e.actor);

// ---- light / dark ----
const SUN = '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>';
const MOON = '<svg viewBox="0 0 24 24"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>';
function mountThemeToggle(btn) {
  const render = () => {
    const dark = document.documentElement.dataset.theme === "dark";
    btn.innerHTML = dark ? SUN : MOON;
    btn.title = dark ? "Switch to light mode" : "Switch to dark mode";
    btn.setAttribute("aria-label", btn.title);
  };
  btn.onclick = () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    store.set("argus.theme", next);
    render();
  };
  render();
}
