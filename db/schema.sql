-- Incident tracker store. The events table is the audit log; the other tables are views of it kept for fast reads.
-- Columns added after the first version are also applied to older databases by Store._migrate().
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL               -- tester | developer | lead
);

CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    service TEXT NOT NULL,
    severity TEXT NOT NULL,
    status TEXT NOT NULL,            -- open | investigating | awaiting_approval | awaiting_qa | resolved | closed | rejected | escalated
    assignee TEXT,
    scenario TEXT NOT NULL,
    mode TEXT NOT NULL,              -- live | replay
    alert TEXT NOT NULL,             -- alert JSON (for tester reports, a synthetic alert built from the report)
    attempts INTEGER NOT NULL DEFAULT 0,
    model TEXT,
    escalation TEXT,
    report TEXT,                     -- markdown
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    source TEXT NOT NULL DEFAULT 'alert',   -- alert | tester_report
    reported_by INTEGER REFERENCES users(id),
    assignee_id INTEGER REFERENCES users(id),
    repro_steps TEXT, expected TEXT, actual TEXT, environment TEXT, log_snippet TEXT
);

CREATE TABLE IF NOT EXISTS claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id INTEGER NOT NULL REFERENCES incidents(id),
    claim_key TEXT NOT NULL,         -- C1, C2 ... as the agents see them
    category TEXT NOT NULL,
    statement TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    verdict TEXT,                    -- null until the verifier answers
    reasoning TEXT,
    UNIQUE (incident_id, claim_key)
);

CREATE TABLE IF NOT EXISTS evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id INTEGER NOT NULL REFERENCES claims(id),
    role TEXT NOT NULL,              -- claim | verdict
    source TEXT NOT NULL,
    line INTEGER NOT NULL,
    quote TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS patches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id INTEGER NOT NULL REFERENCES incidents(id),
    fix_type TEXT NOT NULL,
    rationale TEXT NOT NULL,
    rollback_to TEXT,
    diff TEXT NOT NULL,
    sandbox TEXT,                    -- SandboxResult JSON
    passed INTEGER
);

-- One row per human vote. Sev 1/2 need two distinct approvers, at least one of them a lead.
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id INTEGER NOT NULL REFERENCES incidents(id),
    patch_id INTEGER REFERENCES patches(id),
    approver_id INTEGER NOT NULL REFERENCES users(id),
    role TEXT NOT NULL,
    decision TEXT NOT NULL,          -- approved | rejected
    reason TEXT,
    ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS qa_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id INTEGER NOT NULL REFERENCES incidents(id),
    tester_id INTEGER NOT NULL REFERENCES users(id),
    result TEXT NOT NULL,            -- verified | still_failing
    note TEXT,
    ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id INTEGER NOT NULL REFERENCES incidents(id),
    actor TEXT NOT NULL,
    type TEXT NOT NULL,
    payload TEXT NOT NULL,
    ts TEXT NOT NULL,
    actor_id INTEGER REFERENCES users(id),
    actor_role TEXT
);

CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id INTEGER NOT NULL REFERENCES incidents(id),
    author TEXT NOT NULL,
    text TEXT NOT NULL,
    ts TEXT NOT NULL,
    author_id INTEGER REFERENCES users(id)
);

-- Who is paged first for each service; the lead is the escalation contact.
CREATE TABLE IF NOT EXISTS on_call (
    service TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS events_by_incident ON events (incident_id, id);

-- Small key/value switches that must survive a restart (e.g. Argus paused).
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL              -- JSON
);
