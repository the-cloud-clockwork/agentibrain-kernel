-- Schema template — {{EMBED_DIM}} is substituted by db._ensure_schema() from
-- the configured embedding model's dimension. The HNSW index is created in
-- Python (only when dim <= 2000, pgvector's HNSW ceiling).
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS content_embeddings (
    id SERIAL PRIMARY KEY,
    key VARCHAR(500) NOT NULL,
    chunk_idx INT NOT NULL DEFAULT 0,
    producer VARCHAR(100) NOT NULL,
    content_type VARCHAR(100) DEFAULT 'text/plain',
    text_preview VARCHAR(1000),
    metadata JSONB DEFAULT '{}',
    embedding vector({{EMBED_DIM}}),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(key, chunk_idx)
);

CREATE INDEX IF NOT EXISTS idx_ce_producer ON content_embeddings (producer);
CREATE INDEX IF NOT EXISTS idx_ce_key ON content_embeddings (key);
CREATE INDEX IF NOT EXISTS idx_ce_metadata ON content_embeddings USING gin (metadata);
