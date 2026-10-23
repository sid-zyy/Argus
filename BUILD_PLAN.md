# Incident Commander AI: Build Plan (for 24 Oct 2026, 10:00 to 17:00)

Domain: IT Infrastructure. Track 01: Multi-agent systems.
Team: Siddhartha (agents and orchestration), Yuvika (practice app, sandbox, evaluation, demo).

> Rule: the event forbids pre-built projects. No code before 10:00 on the 24th unless the organizers confirm prep is allowed. This file is planning only.

## 1. What we are building (one paragraph)
An alert plus a service's logs and git history go in. Investigator agents propose a root cause as a list of **claims**. A separate **Verifier** agent tries to refute each claim using tools and must cite evidence. Only verified claims lead to a fix, which is tested in an isolated Docker sandbox. A human approves before anything is "applied", and the system outputs an evidence-backed incident report.

Hackathon pipeline mapping:
| Required stage | Our component |
|---|---|
| Data ingestion | Alert JSON + synthetic logs + git history loaded into tools |
| Intelligent decision-making | Investigator -> Verifier loop, hypothesis ranking |
| Action execution | Patch generated and tested in sandbox; applied only after approval |
| Outcome reporting | Incident report (markdown/HTML) with evidence per claim |
| Responsible AI / human oversight | Approval gate, 3-attempt stop rule, sandbox isolation, evidence-required verdicts, synthetic data only |

## 2. The demo scenario (MVP)
**Toy service:** `checkout-api` (FastAPI) with a Postgres-like DB stub and a connection pool.

**Incident 1 (the main demo): connection leak**
- Alert: "checkout-api 5xx rate 38%, started 14:07".
- Loudest log: `DBConnectionTimeout: pool exhausted` (misleading: points at the database).
- **Wrong first hypothesis (planted trap):** "Database is overloaded, scale it up."
- Verifier refutes it: DB CPU/latency logs are normal, and the errors start 2 minutes after deploy `a1b2c3`.
- Real cause: commit `a1b2c3` added an early `return` in the payment-error path that skips `conn.release()`.
- Fix: release the connection in a `finally`. Sandbox replays failing requests: before = pool exhausted, after = healthy.

**Incidents 2 and 3 (for the "3 of 3 planted mistakes" measure; build if time allows):**
- #2 Config change: `REQUEST_TIMEOUT` changed 30 -> 3 in a deploy; the misleading clue is "upstream payment provider slow".
- #3 Unbounded in-memory cache causing OOM restarts; the misleading clue is "traffic spike".

Make each incident a folder of data (logs, git repo, alert, expected answer) so adding one is cheap.

## 3. Architecture

```
alert.json ─┐
logs/*.log ─┼─> Orchestrator (Python state machine)
git repo ───┘        │
                     ├─> Triage Agent ──> hypotheses[] (claims with evidence refs)
                     ├─> Code-History Agent ──> suspect commits
                     ├─> Verifier Agent (separate context) ──> verdict per claim
                     │        └─ deterministic evidence check (cited line must exist)
                     ├─> Fix Agent ──> patch.diff
                     ├─> Sandbox Runner (Docker) ──> test result
                     ├─> HUMAN APPROVAL GATE
                     └─> Reporter Agent ──> incident_report.md + trace.json
                              │
                  FastAPI event stream (SSE) ──> Incident Dashboard (web)
```

### 3b. Incident tracker app ("Jira for incidents, with an AI first responder")
Positioned as a real product: when an alert fires, a ticket is created automatically, the agents work it, and the human reviews and approves. The same backend also powers a terminal view, so the pipeline never depends on the UI.

**Ticket lifecycle**
`Open` -> `Investigating` (agents running) -> `Awaiting approval` (verified claim + tested patch ready) -> `Resolved` (approved) ; or `Escalated` (3-attempt stop, or Verifier returns "insufficient"), or `Rejected` (human declined, reason logged).

**Pages (single-page web app, FastAPI + static HTML/JS, no heavy framework)**
1. **Board**: kanban columns by status; each card shows ID (INC-101), title, service, severity, time open, AI status. New alerts appear live. Click a card to open the ticket.
2. **Ticket page (the war room)**:
   - Header: ID, severity, service, status, assignee, attempt counter (n/3), model in use.
   - Alert details and timeline.
   - **Claim cards**: statement + cited evidence; flip to Supported / Refuted / Insufficient with quoted log lines.
   - **Suspect commit panel**: diff with bad lines highlighted.
   - **Sandbox result**: before-patch vs after-patch outcome.
   - **Approval panel**: Approve / Reject with reason. Nothing is marked applied until approved.
   - **Report tab**: generated incident report, downloadable.
   - **Activity / audit log**: every agent step and human action with timestamps.
   - **Comments**: simple notes from engineers.
3. **"Trigger test alert" button** (demo control): fires a chosen scenario so we can start incidents on stage.

**Data model (SQLite, via `sqlite3` or SQLModel)**
`incidents(id, title, service, severity, status, assignee, created_at, resolved_at)`,
`claims(id, incident_id, statement, verdict, reasoning)`,
`evidence(id, claim_id, source, line, quote)`,
`patches(id, incident_id, diff, sandbox_before, sandbox_after)`,
`events(id, incident_id, actor, type, payload, ts)` (the audit log),
`comments(id, incident_id, author, text, ts)`.

**Backend endpoints**
- `POST /alerts` (webhook-style intake; creates an incident and starts the agents)
- `GET /incidents`, `GET /incidents/{id}`
- `GET /incidents/{id}/events` (SSE stream: `claim`, `verdict`, `patch`, `sandbox`, `awaiting_approval`, `report`)
- `POST /incidents/{id}/approve`, `/reject`, `/comment`, `PATCH /incidents/{id}` (assignee, severity)
- `GET /incidents/{id}/report`
- `POST /demo/trigger/{scenario}`

**Build order**: (1) pipeline + `rich` terminal view with y/n approval; (2) SQLite + endpoints; (3) ticket page; (4) board; (5) comments/assignee polish. Replay mode feeds `replay/saved_run.json` through the same event stream, so the app works with no network.

**Scope guard**: no real login (roles are a switcher, see 3c), drag-and-drop, search, or editable workflows. If time is short, ship the board + ticket page only; comments and assignee are the first things cut.

**Future work (roadmap slide, don't build)**: VS Code extension as a thin client over the same API; real alert intake from Prometheus/Datadog/PagerDuty webhooks; Slack/Teams notifications; role-based approvals; learning from past resolved incidents.

### Agents (each is a Claude call with its own system prompt and tool list)
| Agent | Job | Tools | Output |
|---|---|---|---|
| Triage | Read alert, search logs, rank hypotheses | `search_logs`, `get_metrics`, `read_lines` | `claims[]` |
| Code-History | Correlate onset time with deploys/commits | `git_log`, `git_diff`, `git_show` | suspect commits + reasoning |
| Verifier | Try to **disprove** each claim; independent context, sees claims and tools but not the Triage chain of thought | same read-only tools | `verdict` per claim |
| Fix | Propose minimal patch for verified cause | `read_file`, `write_patch` | unified diff |
| Reporter | Write the final report from the trace | none | markdown |

Models: see section 3a. No paid key is assumed; everything goes through a provider-agnostic `llm.py`.

### 3c. Roles and tester-reported bugs
Two ways a ticket starts: an automatic **alert** (monitoring) or a **bug report** filed by a tester. Both go through the same agent pipeline; the bug report just adds human-written context.

**Roles (role switcher in the UI, no real login; the API checks the role on every call)**
| Role | Can do |
|---|---|
| **Tester / Reporter** | File a bug report (title, steps to reproduce, expected vs actual, environment, optional log snippet); comment; **confirm the fix** ("verified fixed" or "still failing") |
| **Developer / On-call engineer** | View everything; assign; comment; approve or reject fixes for sev 3 and sev 4; re-run the agents |
| **Lead / Approver** | Everything a developer can do, plus **approve sev 1 and sev 2 fixes** (second approval); reassign; see the metrics view |
| **AI agent** (system actor, not a login) | Creates claims, verdicts, patches and reports; can never approve or apply anything |

Rule: the person who approves cannot be the same person who reported the bug for sev 1 and sev 2 (separation of duties). Show this in the pitch as a responsible-AI control.

**Ticket lifecycle with roles**
`Open` -> `Investigating` (agents) -> `Awaiting approval` (developer, or developer + lead for sev 1 and 2) -> `Applied` (simulated) -> `Awaiting QA` (only for tester-reported bugs) -> `Closed` when the tester confirms. If the tester says "still failing", the ticket goes back to `Investigating` and the attempt counter increments. Escalated and Rejected exits stay as before.

**How a tester report feeds the agents**
- The report text (steps, expected vs actual) becomes an extra input to Triage and is attached as evidence source `report`.
- The Verifier treats the report as a claim source, not as truth. A tester's guess at the cause still has to be supported by logs or code.
- If the report has no matching logs, the Verifier returns "insufficient" and the ticket asks the tester for more information.

**Schema changes (additions to the 3b data model)**
```sql
users(id, name, role)                      -- role in: tester, developer, lead
incidents + source            -- 'alert' | 'tester_report'
incidents + reported_by       -- users.id, null for alerts
incidents + assignee_id       -- users.id
incidents + repro_steps, expected, actual, environment   -- null for alerts
approvals(id, incident_id, patch_id, approver_id, required_role, decision, reason, ts)
qa_checks(id, incident_id, tester_id, result, note, ts)   -- 'verified' | 'still_failing'
events + actor_id, actor_role -- audit log now records who and in what role
comments + author_id
```
`approvals` holds one row per required approval. A sev 1 or 2 ticket needs two rows with decision `approved` (developer and lead) before the status moves on.

**Extra endpoints**
- `POST /bugs` (tester files a report; creates the ticket and starts the agents)
- `POST /incidents/{id}/qa` (tester verifies or rejects the fix)
- `GET /users`, `POST /session/role` (role switcher for the demo)
- Existing approve and reject endpoints now check role and severity.

**Extra pages and UI**
- "Report a bug" form (visible to testers).
- Role switcher in the header.
- Approval panel shows who has approved and who is still needed.
- "Verify fix" buttons on the ticket for the tester.

**Demo addition (about 30 seconds):** switch to Tester, file a bug, switch to Developer to approve, switch to Lead for the second sev 1 approval, switch back to Tester to confirm. Do this only if the core demo is done and rehearsed.

**Scope guard:** build alert-sourced tickets with a single Developer approval first. Add the Tester report form next, then the Lead second approval, then QA verification. If time is short, keep only the three roles and the report form.

### 3d. Scope of faults: code and deployment
Tickets cover code bugs **and** deployment/config problems. Deployment is simulated through readable artifacts, not real infrastructure.

| Category | Evidence sources | Fix type | Scenario |
|---|---|---|---|
| `code` | git diff, stack traces | code patch | Incident 1: connection leak |
| `config` | `deploy.log`, config diffs, env vars | config change or rollback | Incident 2: timeout 30 -> 3 |
| `resource` | memory/CPU metrics, restart events | config limit or code patch | Incident 3: unbounded cache -> OOM |
| `dependency` | upstream health logs | escalate to human (agent can't fix) | optional ambiguous case |

- Each scenario folder has `deploy.log` (version, timestamp, deployer), config files tracked in git, and a metrics file.
- Claims carry a `category`; add it to the claim schema, the claim card, and the eval.
- The Fix agent returns `fix_type` = `patch` | `config_change` | `rollback`; the sandbox tests all three. Prefer rollback when the latest deploy is the proven cause (lowest risk).
- Tester bug reports describe symptoms only; the agents assign the category.
- Out of scope (roadmap slide): real Kubernetes/cloud/networking/CI-CD integration.

### 3e. How each agent works, and why this is not just prompting
**Common agent loop** (same code for Triage, Code-History, Verifier, Fix):
```
messages = [system_prompt, task]
for step in range(MAX_STEPS):            # e.g. 8, hard cap
    reply = llm.chat(messages, tools)
    if reply.tool_calls:
        results = run_tools(reply.tool_calls)   # real code runs, results go back to the model
        messages += [reply, results]
    else:
        output = validate(reply, schema)        # Pydantic; retry if invalid
        return output
```
The model decides which tool to call next based on what it just saw. Our code runs the tool, enforces the step cap and validates the output.

**Per agent**
| Agent | Goal | Typical steps | Decides | Done when | Cannot |
|---|---|---|---|---|---|
| Triage | Explain the alert | grep for error spikes, read surrounding lines, check metrics, check what changed near onset | Which hypotheses to form and rank | Returns 1 to 3 claims, each with cited evidence | Write files, run the sandbox |
| Code-History | Find the change that caused it | `git_log` near onset time, `git_diff` on suspects, read `deploy.log` | Which commit or config change matches the timing and the symptom | Returns suspect commits with reasons | Modify the repo |
| Verifier | Try to disprove each claim | For each claim, look for evidence that contradicts it (normal DB metrics, wrong timing, errors before the deploy) | `supported` / `refuted` / `insufficient` | Verdict per claim with cited evidence that passes the code check | See Triage's reasoning, only the claims |
| Fix | Smallest safe change | Read the file, write the patch, optionally pick rollback instead | patch vs config change vs rollback | Returns a diff or rollback target | Apply it, only propose |
| Reporter | Explain to humans | Summarize trace, evidence, test result | What to highlight | Markdown report | Invent facts not in the trace |

**Handoff and adaptation:** each agent's output is typed JSON. The orchestrator passes it on and feeds refutations back to Triage ("claim C1 was refuted because ...; find another cause"), so the plan changes based on results. Sandbox failure feeds back to Fix.

**What differs from prompting a chatbot with the logs**
| Chatbot / single prompt | This system |
|---|---|
| Whole log pasted in one prompt, or the user digs out snippets | Agent searches and reads only what it needs, step by step, using tools |
| One answer, no check | A separate Verifier tries to disprove it, and the answer changes if it fails |
| Can quote evidence that does not exist | Code checks every cited line and quote against the real file; fake evidence is rejected |
| Suggests a fix in text | Fix is run in an isolated sandbox: fails before, passes after |
| No control over what happens next | Approval gate, role checks, 3-attempt stop, audit log |
| Free text, hard to track | Structured claims, verdicts and tickets saved in a database |
| Cannot change course | Refutation or test failure triggers a retry with new information |

**Why not a fixed script?** The scenarios have different evidence, so the investigation path differs each time (log grep vs deploy diff vs memory metrics). The model chooses the path; the code controls the safety.

**Be honest in the pitch:** the Verifier is another LLM, so it can also be wrong. That is why the evidence check, the sandbox test and the human approval sit after it. Say "reduces and catches wrong answers", not "eliminates them".

**Ablation for the demo or Q&A (cheap to run, strong evidence):** run the same incident with (a) a single prompt containing the logs and (b) our pipeline, and compare: did it name the right cause, did it cite real evidence, did the fix pass the sandbox. Report only what you actually measure.

### 3a. Model provider strategy (no paid API key)
Platform choice is open per the deck notes. If we end up not using Claude, update slide 5 and the references slide to name the real model and provider.

| Role | Provider | Why |
|---|---|---|
| Primary: Triage, Verifier, Fix | **Gemini free tier** | Best tool calling and structured JSON of the free options; large context |
| Fast and light: Reporter, retries; failover when Gemini rate-limits | **Groq free tier** (open model such as Llama or Qwen) | Very fast; weaker reasoning, so not the Verifier |
| Offline fallback and dev without limits | **Ollama** (local, 32 GB RAM laptop) | Free, no rate limits; speed depends on GPU (CPU-only is slow) |
| Optional | Claude via API, only if credits are provided | Not required |

Groq free tier (checked 23 Oct 2026 on console.groq.com/docs/rate-limits; confirm on your account's Limits page): text models such as `openai/gpt-oss-120b`, `openai/gpt-oss-20b`, `qwen/qwen3.8-27b` get **30 requests/min, 1K requests/day, 8K tokens/min, 200K tokens/day**. 8K tokens/min is tight: an agent call carrying about 3K tokens of context allows only 2 to 3 calls per minute. Keep prompts small, read the `x-ratelimit-remaining-tokens` header, and back off on HTTP 429 using `retry-after`.

Laptop check (23 Oct): Intel Iris Xe integrated graphics, no dedicated GPU, 32 GB RAM. Ollama works only on CPU here, so use it for offline dev, not the live demo.

`llm.py` interface:
```python
chat(messages, tools=None, schema=None, provider=None) -> dict
```
- Provider chosen by config (`LLM_PROVIDER`, `LLM_MODEL`); use an OpenAI-compatible client where the provider supports it.
- Every response is validated against a Pydantic schema; on bad JSON, retry up to 2 times, then fall back to the next provider.
- Handle rate limits with a short backoff, then fail over to the next provider.
- Keys come from environment variables or `.env` (in `.gitignore`).
- Keep prompts small: grep the logs in code and send only matching lines. Free tiers have token caps.
- Verify the pass/fail behaviour of the Verifier on the weakest provider you plan to demo on, not just the best one.

### Data contracts (define these first, in the first 30 minutes)
```json
// Claim
{"id": "C1", "category": "code|config|resource|dependency", "statement": "...", "evidence": [{"source": "app.log", "line": 412, "quote": "..."}]}

// Verdict
{"claim_id": "C1", "verdict": "supported|refuted|insufficient",
 "reasoning": "...", "evidence": [{"source": "db.log", "line": 88, "quote": "..."}]}
```

### The key trick: evidence is checked by code, not trusted
After the Verifier answers, a deterministic function checks that every cited `source:line` exists and that the `quote` appears in that line. A verdict with fake or missing evidence is rejected and sent back. This is what makes "no claim without proof" real, and it is the main answer to "why not just a chatbot".

### Orchestrator loop
```
attempt = 0
while attempt < 3:
    claims = triage(alert, feedback)
    verdicts = verifier(claims)           # evidence-checked
    if any supported: break
    feedback = refutations; attempt += 1
else: STOP -> escalate to human with everything gathered
patch = fix(supported_claim)
result = sandbox(patch)                   # replay failing requests, run tests
if result.fail: attempt += 1, retry fix (max 3 total)
request_human_approval(report_draft)      # CLI prompt or UI button
if approved: mark "applied" (simulated) ; else: log reason
write incident_report.md and trace.json
```

### Safety controls (say these out loud in the pitch)
- Human approves every fix; nothing is auto-applied.
- Stop rule: 3 failed attempts -> escalate.
- Sandbox: Docker with no network, a read-only mount of the repo, CPU/memory/time limits.
- Tools are read-only except the sandbox; the agent never touches the real repo.
- Synthetic data only; no secrets in prompts or logs.
- Every decision is logged to `trace.json` for audit.

## 4. Repo layout
```
incident-commander/
  orchestrator/   main.py, state.py, approval.py
  agents/         triage.py, code_history.py, verifier.py, fix.py, reporter.py, prompts/
  tools/          logs.py, git_tools.py, metrics.py, sandbox.py, evidence_check.py
  scenarios/      incident1_conn_leak/{alert.json, logs/, repo/, expected.json}
                  incident2_timeout/ ...  incident3_cache_oom/ ...
  sandbox/        Dockerfile, replay.py
  ui/             FastAPI app + static tracker UI (board.html, ticket.html, app.js, style.css), SSE events
  db/             schema.sql, models.py (SQLite), seed_users.py (3 demo users: tester, developer, lead)
  eval/           run_eval.py  (scores catch rate across scenarios)
  replay/         saved_run.json  (fallback demo)
  README.md
```

## 5. Hour-by-hour schedule (7 hours; assume about 5 of them are real coding)
| Time | Siddhartha (agents) | Yuvika (app, sandbox, demo) |
|---|---|---|
| 10:00 to 10:30 | Set up repo, API key, agree on the Claim/Verdict contracts together | Same; set up Docker and the toy-app skeleton |
| 10:30 to 12:00 | Tool functions for logs and git, plus the Triage agent (end to end on incident 1) | Toy `checkout-api`, the leak commit in git history, synthetic logs that include the misleading clue |
| 12:00 to 13:00 | Verifier agent + evidence checker | Docker sandbox + replay script (fails before patch, passes after) |
| 13:00 to 13:30 | Lunch / buffer | Lunch / buffer |
| 13:30 to 15:00 | Fix agent, orchestrator loop, 3-attempt stop, approval gate | Incidents 2 and 3 data; eval script |
| 15:00 to 16:00 | Reporter + trace; emit typed events; **record `replay/saved_run.json`** from a good run | SQLite + endpoints, ticket page with claim cards and approve button, then the board |
| 16:00 to 16:45 | Bug fixes, run the demo 3 times end to end | Same; rehearse |
| 16:45 to 17:00 | Freeze. No new features. | Final checks |

**Cut order if behind:** board/comments/assignee polish (fall back to ticket page only, or the `rich` terminal view with y/n approval) -> incidents 2 and 3 -> Reporter polish. Never cut the Verifier, the evidence check, the sandbox or the approval gate.

## 6. Evaluation (matches the deck's success measure)
- Run all scenarios; for each, record: did the Verifier catch the planted wrong hypothesis (yes/no), did the final fix pass the sandbox, how many attempts, how long it took.
- Report honestly as "caught N of 3". The deck says these are targets, not measured results, so don't claim numbers you didn't measure.
- Include one ambiguous case (insufficient evidence -> escalates to the human) and one failure case (the agent hits the 3-attempt stop). This shows the limits are handled.

## 7. Demo script (about 3 to 4 minutes)
1. Alert fires: "checkout-api 5xx 38%". (15s)
2. Triage says "DB overloaded" and the dashboard or terminal shows the claim. (30s)
3. Verifier **rejects** it with cited evidence: DB metrics are normal, errors start after the deploy. (45s)
4. Agents find commit `a1b2c3` and the missing `release()`. (30s)
5. Fix is tested in the sandbox: fails before, passes after. (30s)
6. Human clicks **Approve**. (15s)
7. Show the incident report with evidence per claim. (30s)
8. Close with the scorecard (caught N of 3) and the safeguards. (30s)

Fallback: if the API or network fails, run `replay/saved_run.json` through the same UI or CLI.

## 8. Risks and mitigations
| Risk | Mitigation |
|---|---|
| LLM output not reproducible on stage | Planted trap is deterministic in the data; temperature low; saved replay as backup |
| Verifier rubber-stamps the claim | Separate context, told to disprove, plus the deterministic evidence check |
| Free-tier rate limits or quota hit mid-demo | Failover Gemini -> Groq -> Ollama in `llm.py`; cap context size (grep first); cache responses by input hash; saved replay as final backup |
| Weaker models give invalid JSON or skip tools | Pydantic validation + retries; narrow prompts with 2 to 3 tools per agent; deterministic evidence check |
| Sandbox trouble on Windows | Test Docker Desktop at 10:00; fall back to a subprocess with a temp directory and timeouts |
| Scope creep | Freeze at 16:45; MVP is incident 1 only |

## 9. What you can do before the event without building the project
- Ask the organizers whether prep is allowed (Induj Gupta, +91 95694 06171).
- Confirm Docker Desktop, Python 3.11 and Git run (simple "hello" tests, not project code).
- Create free keys for **Gemini** and **Groq**, and send one test call to each. Note the current rate limits.
- Install **Ollama** and pull one small tool-capable model as an offline fallback. Note whether the laptop has a dedicated GPU; CPU-only will be slow.
- Ask in the WhatsApp group whether CGI or MIT provides API credits.
- Read the tool-calling docs for Gemini and Groq.
- Update the deck: add "Domain: IT Infrastructure" and name the report as the "outcome reporting" step.
- Split the pitch parts and rehearse the demo script above.
- Join the WhatsApp group.
