-- pgvector embeddings table used by the embeddings service.
--
-- This schema is the authoritative one; the embeddings service also ships
-- services/embeddings/src/init.sql with IDENTICAL DDL that it auto-applies at
-- startup via ``_ensure_schema``. The two must stay in sync — if you edit
-- either, edit both.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS content_embeddings (
    id            SERIAL PRIMARY KEY,
    key           VARCHAR(500) NOT NULL,
    chunk_idx     INT NOT NULL DEFAULT 0,
    producer      VARCHAR(100) NOT NULL,
    content_type  VARCHAR(100) DEFAULT 'text/plain',
    text_preview  VARCHAR(1000),
    metadata      JSONB DEFAULT '{}',
    embedding     vector(1536),
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (key, chunk_idx)
);

CREATE INDEX IF NOT EXISTS idx_ce_embedding_hnsw
    ON content_embeddings USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
CREATE INDEX IF NOT EXISTS idx_ce_producer ON content_embeddings (producer);
CREATE INDEX IF NOT EXISTS idx_ce_key ON content_embeddings (key);
CREATE INDEX IF NOT EXISTS idx_ce_metadata ON content_embeddings USING gin (metadata);
