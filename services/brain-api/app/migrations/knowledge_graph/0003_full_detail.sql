CREATE TABLE IF NOT EXISTS knowledge_details (
    entity_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    document TEXT NOT NULL,
    PRIMARY KEY (entity_id, revision)
);
