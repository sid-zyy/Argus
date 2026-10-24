# Argus Connect: config reference

A connected service is a folder under `scenarios/` with an `argus.yaml`. Paths in it are relative to that file (or
absolute). A folder without one uses the original recorded-scenario layout (`alert.json`, `logs/*.log`, `repo/`,
`repro.py`). Full examples: [`scenarios/quickbite_orders/argus.yaml`](../scenarios/quickbite_orders/argus.yaml) (Node.js,
Grafana, JSON logs) and [`scenarios/live_shop/argus.yaml`](../scenarios/live_shop/argus.yaml) (Python, Alertmanager, text logs).

```yaml
version: 1
service:
  name: orders-service          # what alerts call it (required)
  label: QuickBite ordering     # for people
  team: Ordering team
  language: javascript
  on_call: Meera                # an Argus user; used when the on-call roster has no one for this service

repo:
  adapter: git
  path: repo                    # a local checkout, or
  # url: https://github.com/acme/orders.git   # cloned once into runs/repos/<folder>

alerts:
  adapter: grafana              # alertmanager | grafana | standard
  match: {service: orders-service}   # labels an alert must carry (default: service = service.name)
  base: alert.json              # standard-format fields a webhook leaves out (metric, threshold, window)
  sample: alerts/grafana_alert.json  # an example payload, used by the checks and the "Send test alert" button
  severity_map: {warning: sev3, critical: sev1}

logs:                           # each becomes a source the agents search, read and cite by `name`
  - name: app.log
    adapter: jsonl              # text | jsonl | csv | command
    path: logs/app.jsonl
    role: service               # service | metrics | dependency | deploy | other
    fields: {time: time, level: level, message: msg}
    time_format: epoch_ms       # iso | epoch_ms | epoch_s
    level_names: {"30": INFO, "50": ERROR}
    drop: [pid, hostname]       # keys left out of the rendered line
  - {name: payrail.log, adapter: text, path: logs/payrail.log, role: dependency, dependency: PayRail}
  - {name: metrics.log, adapter: csv, path: logs/metrics.csv, role: metrics, fields: {time: timestamp}}
  - {name: deploy.log, adapter: jsonl, path: logs/deploys.jsonl, role: deploy, fields: {time: ts, message: event}}
  # - {name: pod.log, adapter: command, run: "kubectl logs deploy/orders --since=2h", role: service}

metrics:
  source: metrics.log           # default: the first source with role metrics
  description: What each metric field means (shown to the agents in the get_metrics tool).

dependencies:
  - {name: PayRail payment gateway, kind: external, logs: [payrail.log], note: Charges cards}

commands:                       # run in the sandbox on a clone of the repo; {repo} is the clone
  files: [checks/repro.js]      # copied next to the clone
  repro: node checks/repro.js {repo}        # exit 0 = healthy, non-zero = the failure reproduces
  test: node --test "{repo}/test/*.test.js"
  test_if_exists: "{repo}/test" # optional: only run tests when this exists
  image: node:22-alpine         # Docker image when SANDBOX_MODE=docker
  timeout: 60

severity:
  default: sev3
  rules:                        # first match wins; beats the alert's own severity
    - {metric: order_failure_rate, above: 0.25, severity: sev1}
  approvals:                    # sev1..sev4 or default; beats TWO_PERSON_APPROVAL
    sev1: {count: 1, lead: true}
    default: {count: 1}

deploy:
  on_approve: false             # true: commit the approved fix to the repo (its pipeline deploys it)
  app: QuickBite
  url: http://localhost:8200

business: business.json         # revenue-at-risk assumptions (see README, Business impact)
```

## Why log roles matter
The fact checker only accepts a root cause backed by the cause itself. Roles tell it which sources are which:
- `service` and `metrics` sources show symptoms. They are never enough on their own to confirm a cause.
- A `dependency` source is a dependency's own signal, so it can support a "the gateway is down" claim.
- A `deploy` source counts as evidence of a change, like a commit or a code line.

Name the deploy source `deploy.log`: the change finder's prompt looks for it by that name.

## The standard incident
Every alert adapter produces an `IncidentInput` (`connect/schema.py`, or `GET /connect/schema`): `id`, `service`,
`title`, `severity` (sev1..sev4, or a common word such as critical/high/warning), `metric`, `value`, `threshold`,
`window`, `started_at`, `fired_at`, `source`, `description`, `labels`. Post it directly to `POST /alerts` from any tool
that is not Alertmanager or Grafana.

## Checks
`python -m connect.wizard check <folder>` runs, and every incident start repeats, the quick checks:
- the config is valid;
- the repo exists and has history;
- the base alert fits the schema;
- the sample payload is in the adapter's format and routes to this service;
- every log source exists, parses, and starts lines with a timestamp;
- metric and dependency log names are real;
- the commands' programs are installed and their files exist;
- the policy keys are valid;
- the business file and service map are present.

`--full` also clones the repo and runs the repro and tests once, the way a fix will be tested. An incident is refused
while any check fails.
