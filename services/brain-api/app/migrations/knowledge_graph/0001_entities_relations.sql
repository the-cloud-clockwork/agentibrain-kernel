CREATE TABLE IF NOT EXISTS graph_entities (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    revision INTEGER NOT NULL,
    projects TEXT NOT NULL,
    vault_ref TEXT,
    title TEXT NOT NULL,
    provenance TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS graph_relations (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL REFERENCES graph_entities (id),
    type TEXT NOT NULL,
    target TEXT NOT NULL REFERENCES graph_entities (id),
    status TEXT NOT NULL,
    confidence TEXT NOT NULL,
    evidence TEXT NOT NULL,
    UNIQUE (source, type, target)
);

CREATE INDEX IF NOT EXISTS graph_relations_target ON graph_relations (target);

CREATE TABLE IF NOT EXISTS graph_quarantine (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    reason TEXT NOT NULL,
    record TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS graph_invalid_relations (
    reason TEXT PRIMARY KEY,
    total INTEGER NOT NULL
);
