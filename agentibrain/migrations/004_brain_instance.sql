-- The brain that owns this database: one row, claimed by the first migration
-- run. run_migrations passes brain_id and refuses a database another brain holds.

CREATE TABLE IF NOT EXISTS brain_instance (
    singleton  BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    brain_id   TEXT NOT NULL,
    claimed_at TIMESTAMPTZ DEFAULT now()
);

INSERT INTO brain_instance (brain_id) VALUES (:'brain_id') ON CONFLICT (singleton) DO NOTHING;
