-- Cognitive-tick bookkeeping. Used by the tick-engine to track last-run state
-- and detect missed ticks.

CREATE TABLE IF NOT EXISTS tick_runs (
    id          BIGSERIAL PRIMARY KEY,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    status      TEXT NOT NULL DEFAULT 'running',   -- running|ok|partial|failed
    mode        TEXT NOT NULL DEFAULT 'scheduled', -- scheduled|manual
    stats       JSONB NOT NULL DEFAULT '{}'::jsonb,
    error       TEXT
);

CREATE INDEX IF NOT EXISTS tick_runs_started_at_idx ON tick_runs (started_at DESC);
CREATE INDEX IF NOT EXISTS tick_runs_status_idx ON tick_runs (status);

CREATE TABLE IF NOT EXISTS signal_state (
    key           TEXT PRIMARY KEY,   -- signal identity (source + kind + hash)
    severity      TEXT NOT NULL,      -- nuclear|critical|warning|info|resolved
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at   TIMESTAMPTZ,
    payload       JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS signal_state_severity_idx ON signal_state (severity);
CREATE INDEX IF NOT EXISTS signal_state_resolved_at_idx ON signal_state (resolved_at);
