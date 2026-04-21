-- pgvector embeddings table used by the embeddings service and brain arcs.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS content_embeddings (
    id            BIGSERIAL PRIMARY KEY,
    producer      TEXT NOT NULL,
    content_key   TEXT NOT NULL,
    chunk_index   INT  NOT NULL DEFAULT 0,
    embedding     vector(1536) NOT NULL,
    text_preview  TEXT,
    metadata      JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (producer, content_key, chunk_index)
);

CREATE INDEX IF NOT EXISTS content_embeddings_producer_idx
    ON content_embeddings (producer);
CREATE INDEX IF NOT EXISTS content_embeddings_content_key_idx
    ON content_embeddings (content_key);
-- ivfflat index built after table has data; leave to an operational step.
