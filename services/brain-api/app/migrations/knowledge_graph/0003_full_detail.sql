CREATE TABLE IF NOT EXISTS knowledge_details (
    entity_id TEXT NOT NULL REFERENCES graph_entities(id),
    revision INTEGER NOT NULL,
    document TEXT NOT NULL,
    PRIMARY KEY (entity_id, revision)
);
