# Argus: multi-agent incident commander

**Business domain: IT Infrastructure.** When a production service breaks, Argus plans an investigation, finds the root
cause with evidence that is checked against the real files, tests a fix on a safe copy, and waits for a person to
approve it. Then it reports what happened and what it cost.

## How it covers the required pipeline
| Stage | What Argus does |
|---|---|
| Data ingestion | Prometheus Alertmanager webhooks (`POST /alerts`), tester bug reports, logs, metrics, git history, deploy records |
| Intelligent decision-making | Athena (Commander) writes a plan; Theia (Investigator) proposes causes; Themis (Fact checker) tries to disprove each one; Chronos (Change finder) finds the commit; the plan is redone when a guess is ruled out |
| Action execution | Hephaestus (Fixer) writes the smallest fix; Talos (Test lab) replays the failure without and with it; after approval the fix is committed and deployed to the live app (simulated for the recorded incidents) |
| Outcome reporting | Business summary for managers, business impact (revenue at risk, time to fix, money protected), evidence, decisions made by people, AI usage |
| Responsible AI | Every quote is checked against the real file; a cause must be backed by the code or change itself, not just symptoms; 3-attempt stop; sandbox; the rules live in `policy.yaml` (allowed fixes, protected paths, change size, AI budget); hash-chained, tamper-evident audit log |
| Human oversight | Nothing changes without a person approving; optionally serious fixes need two people including a team lead; the reporter can't approve their own serious fix; approval deadline escalates to the lead; testers confirm fixes |

## The team
| Name | Role | Job |
|---|---|---|
| Athena | Commander | Plans the investigation, re-plans when a guess is ruled out |
| Theia | Investigator | Reads logs and metrics, proposes causes with quoted evidence |
| Chronos | Change finder | Finds the commit or deploy that lines up with the problem |
| Themis | Fact checker | Tries to prove each cause wrong; only checked evidence counts |
| Hephaestus | Fixer | Writes the smallest fix, a settings change or a rollback |
| Talos | Test lab | Reproduces the failure on a safe copy, then checks the fix makes it go away |
| Clio | Writer | Writes the report for engineers and managers |

## Live demo: break a real app and watch Argus fix it
ShopFast (`shop/`) is a small store whose checkout code lives in a real git repo, `scenarios/live_shop/repo`.
It serves simulated customers plus your own clicks, writes real logs and metrics to `scenarios/live_shop/logs`,
and deploys every new commit within a couple of seconds.
```
python -m shop.server                       # the shop, http://localhost:8100
uvicorn ui.app:app --port 8000 --reload     # Argus, http://localhost:8000
```
1. Open both. The dashboard's ShopFast panel shows its health, live commit and error rate.
2. Break it: click **Push a bad change** (commits the "return early on declined payments" refactor as Arjun), or
   edit `app/checkout.py` in `scenarios/live_shop/repo` yourself and `git commit`.
3. Each declined card now leaks a database connection. In about a minute the pool is empty, checkout fails, and
   ShopFast's monitor sends an Alertmanager webhook to Argus, which opens an incident on its own.
4. The agents investigate the real logs and the real git history, test a fix in the sandbox and ask for approval.
5. Approve it (a developer and the lead for a major incident). Argus commits the fix to the repo, ShopFast deploys
   it, and the error rate drops back to zero. **Reset** puts the shop back to its healthy code and clears its logs.

`ARGUS_URL` (shop) and `SHOP_URL` (Argus) change the addresses; `SHOP_RPS` sets the simulated traffic.

## Argus Connect: the same agents on any service
Argus is not tied to ShopFast. Each service plugs in through **adapters** and one config file, `argus.yaml`, in its
folder under `scenarios/`; the agents, prompts, evidence checker, sandbox and approval gate are the same for all of them.

| Piece | What it does |
|---|---|
| Standard incident (`connect/schema.py`) | The one shape every alert becomes: service, title, severity, metric, value, threshold, window, times, labels |
| Alert adapters | `alertmanager`, `grafana`, `standard`: webhooks to `POST /alerts` are normalised and routed to the service whose `alerts.match` labels they carry |
| Repo adapter | `git`: a local checkout or a URL cloned on first use |
| Log adapters | `text`, `jsonl` (pino, bunyan, CI records), `csv` (metrics exports), `command` (`docker logs`, `kubectl logs`): all rendered as the same numbered, timestamped lines the agents search and quote |
| Service config (`argus.yaml`) | Log roles (service, metrics, dependency, deploy), dependencies, repro and test commands, sandbox image, severity rules, approval policy, on-call, deploy-on-approve |
| Setup wizard (`connect/wizard.py`) | Writes a config from a few questions, then validates it; the same checks gate every incident in the web app and the terminal |

Two different sample apps run through the unchanged pipeline:

| | ShopFast (`live_shop`) | QuickBite (`quickbite_orders`) |
|---|---|---|
| Language | Python | Node.js |
| Monitoring | Prometheus Alertmanager | Grafana Alerting |
| Logs | Plain text | pino JSON lines, CSV metrics, CI deploy records in JSON |
| Repro and tests | `python repro.py`, pytest | `node checks/repro.js`, `node --test` |
| Policy | Global approval rule | Failure rate ≥ 25% is sev1, and sev1 fixes need a team lead |

QuickBite's bug: commit `d9cece1` moves promo codes to `promos.json` with whole-number percentages but still multiplies by
the value, so WEEKEND20 orders go negative and the payment gateway rejects them. The trap: every error says
`GatewayError`, the gateway client changed the day before, and a marketing push started minutes earlier.
```
python -m connect.wizard list                          # every service and whether its config passes
python -m connect.wizard check quickbite_orders --full # validate it, and run the repro and tests once in the sandbox
python -m connect.wizard new my_service                # connect a new service: answer a few questions
python -m connect.wizard route payload.json            # which service an alert would open an incident for
python -m orchestrator.main scenarios/quickbite_orders --alert scenarios/quickbite_orders/alerts/grafana_alert.json
```
The dashboard's **Connect** page (`/connect`) shows each service's adapters and checks, compares the two apps, and
sends a service's sample alert through its adapter. Config reference: [docs/CONNECT.md](docs/CONNECT.md).

## Setup
```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env   # add GROQ_API_KEY and/or GEMINI_API_KEY
```

## Run
```
python run_demo.py                                              # ShopFast + Argus + the self-check, Ctrl+C stops both
python -m ui.health                                             # the self-check on its own (also the Health page, /check)
uvicorn ui.app:app --port 8000 --reload                         # dashboard at http://localhost:8000
python -m orchestrator.main scenarios/incident1_conn_leak -v    # terminal run, asks for approval
python -m orchestrator.main ... --save-replay                   # also write replay/<scenario>.json (enables the Replay button)
python -m eval.compare                                          # Argus vs a single chatbot prompt (real model calls)
python -m eval.run_eval                                         # scorecard across all scenarios (real model calls)
python -m pytest -q tests                                       # offline tests with a scripted fake model
```
Send a real Alertmanager or Grafana webhook:
```
curl -X POST http://localhost:8000/alerts -H "content-type: application/json" -d @examples/alertmanager_webhook.json
curl -X POST http://localhost:8000/alerts -H "content-type: application/json" -d @scenarios/quickbite_orders/alerts/grafana_alert.json
```
Only one Argus server can use a database: it holds a lock on `incidents.db.lock` while it runs, and a second server
on the same file refuses to start (it would otherwise escalate the first one's open incidents). Use `INCIDENT_DB=<path>`
for a second, separate server.

Pages stamp their assets with the file's modification time and send `Cache-Control: no-cache`, so a normal reload
always shows the latest UI. Restart the server after Python changes (or use `--reload`).

## Scenarios
| Scenario | Service | Real cause | Planted trap |
|---|---|---|---|
| `incident1_conn_leak` | checkout-api | early `return` skips `pool.release()` (code) | "database overloaded" |
| `incident2_timeout` | checkout-api | `REQUEST_TIMEOUT` lowered 30 → 3 s (config) | "payment provider is slow" |
| `incident3_cache_oom` | catalog-api | unbounded page cache keyed by full URL (resource) | "marketing traffic spike" |
| `quickbite_orders` | orders-service (Node.js) | promo percentages stored as 20, still multiplied (code) | "the payment gateway is failing" |

Each folder is a **service pack**: `history.py` (commits), `generate_logs.py` (deterministic telemetry), `repro.py` (the
sandbox check), `expected.json` and `business.json` (the business assumptions behind the impact figures); `repo/` and
`logs/` are generated by `python -m scenarios.build <name>`. Adding a service is data, not code
([docs/SERVICE_PACK.md](docs/SERVICE_PACK.md)):
```
python -m scenarios.pack new inventory_retries --service inventory-api   # a working starter pack to edit
python -m scenarios.pack check                                           # validate every pack
```

## Business impact
Figures come from each scenario's `business.json` and are always labelled as estimates; the UI and report list the
assumptions. Revenue at risk = units per minute × share failing × value per unit. Time to fix with Argus runs from the
problem starting to the approval; it is compared with a typical manual fix time. Engineer time saved is priced with
`engineer_cost_per_hour`. AI cost is estimated from tokens at the price in `policy.yaml` (`ai.cost_per_1k_tokens`), so
every incident shows what it cost in AI next to what it saved; AI energy is a rough estimate at 0.3 Wh per 1,000 tokens.
The Value tab of the Results page (`/results#value`) lists every incident, archived ones too, and lets you change any assumption to see how the
totals move.

## Governance: policy.yaml and the audit trail
`policy.yaml` holds the rules in one reviewable file, re-read whenever it changes:
- who must approve a fix at each severity (and the two-person rule for serious incidents), separation of duties;
- which kinds of fix are allowed, paths a fix may never touch, the most lines one fix may change: a fix that passes the
  sandbox but breaks a rule is sent back to the fixer with the rule, and after 3 tries the incident goes to a person;
- the AI budget per run: when it is spent, Argus stops and hands over;
- the approval SLA.

Every approval, block and stop records the rule that applied and the policy file's fingerprint. `GET /policy` and the
Health page show the rules in plain words. A service whose `argus.yaml` has its own `severity.approvals` rule uses that
instead for its incidents (QuickBite: a team lead approves sev1 fixes), recorded the same way with that file's fingerprint.

The audit log is hash-chained: each event's hash covers its fields and the previous event's hash, so editing, deleting
or reordering any past event breaks the chain from there on. Older databases are sealed when first opened.
`GET /audit/verify` (the **Verify** button on the Timeline tab and the Health page) recomputes the whole chain.

## Users, approvals and automation
Demo users (top-right menu, no passwords): Tara (tester), Siddhartha and Meera (developers), Yuvika (team lead).
The chosen user is kept in a signed cookie and the server checks the role on every write.
- New incidents are assigned to the service's on-call developer (checkout-api: Siddhartha, catalog-api: Meera).
- Notifications are simulated as Slack #incidents messages and recorded in the timeline.
- A fix waiting longer than `approval_sla_minutes` in `policy.yaml` (default 10; `APPROVAL_SLA_MINUTES` overrides it) is
  escalated to the lead.
- Testers file bug reports and confirm fixes; developers approve minor fixes; with `two_person.enabled` in `policy.yaml`
  (or `TWO_PERSON_APPROVAL=1`), serious (sev 1/2) fixes need two people, one of them a team lead (off by default: one
  developer or lead approves); the AI never approves; declining needs a reason.

## UI
- Dashboard (`/`): Argus status, business and safety tiles (including return on AI spend), board, recent activity, and
  what waits on the signed-in user.
- Ticket (`/incidents/{id}/view`): progress steps, business impact, the Argus sphere, live progress (plan, current
  helper, its reasoning, free-tier waits, what it read, AI usage), causes with checked evidence, the change that caused
  it, the fix and test result, approvals (with the policy rule that applies), QA, report, timeline with the audit-trail
  check, and notes. Findings also put Argus next to a single chatbot prompt on this incident, scored the same way;
  a developer can ask a chatbot live (one real AI call, kept in the audit log).
- Results (`/results`), two tabs:
  - **Accuracy** (the recorded benchmark): Argus vs a single chatbot prompt on the same incidents, scored the same way:
    a head-to-head scoreboard (including speed, where the chatbot wins), and per incident a race that plays both runs
    back on one clock.
  - **Value** (your incidents, `/results#value`; `/value` redirects there): every incident's value and AI cost, with
    editable assumptions.
- Health (`/check`): the self-check, the policy in plain words and the audit-trail check.
- Everywhere: Ctrl/⌘ K opens a command palette (jump to an incident, start a demo, switch person, pause Argus), `?` lists
  the shortcuts, `g d` / `g r` jump to the dashboard or results. On the dashboard, **Take the tour** (or `T`) walks through
  each part, and a service health strip shows 30 days per service like a status page. Open incidents tick a live clock;
  a fix going through sets off confetti.

## Layout
- `llm.py`: Groq / Gemini / Ollama through OpenAI-compatible APIs, with failover, waits and cooldowns.
- `agents/`: shared tool-calling loop (`base.py`), the agents including the Commander (`investigators.py`), prompts.
- `connect/`: Argus Connect: incident schema, alert/repo/log adapters, `argus.yaml` loader, severity and approval policy, checks, wizard.
- `tools/`: read-only log, metrics and git tools over a service's adapters; `evidence_check.py`; `sandbox.py` (runs the service's own commands).
- `orchestrator/`: state machine (`pipeline.py`), contracts, approval gate, terminal view, report, replay.
- `db/`: schema, store (with the hash-chained audit log), business impact, the single-server lock (`lock.py`).
- `policy.yaml` and `orchestrator/policy.py`: the governance rules. `ui/health.py`: the self-check. `run_demo.py`: one-command start.
- `ui/`: FastAPI app, background runner, static pages. `eval/`: scorecard and comparison.
- `scenarios/pack.py`: service-pack scaffolder and validator.

## Sandbox
`SANDBOX_MODE=auto` uses Docker when it is running (`docker build -t incident-commander-sandbox sandbox/`), with no network
and CPU/memory limits. Without Docker it falls back to a subprocess in a temp copy with a scrubbed environment and a
timeout, which does not isolate the network.
