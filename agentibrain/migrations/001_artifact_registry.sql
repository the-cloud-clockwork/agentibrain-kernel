-- Artifact registry — the lightweight metadata index for blobs in S3/MinIO.
-- Owned by artifact-store in the operator's deployment; the kernel keeps a
-- compatible schema so brain services can read/write against it.

CREATE TABLE IF NOT EXISTS artifacts (
    key         TEXT PRIMARY KEY,
    producer    TEXT NOT NULL,
    type        TEXT NOT NULL,
    content_type TEXT,
    size_bytes  BIGINT,
    tags        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ,
    checksum    TEXT
);

CREATE INDEX IF NOT EXISTS artifacts_producer_idx ON artifacts (producer);
CREATE INDEX IF NOT EXISTS artifacts_type_idx ON artifacts (type);
CREATE INDEX IF NOT EXISTS artifacts_created_at_idx ON artifacts (created_at DESC);
CREATE INDEX IF NOT EXISTS artifacts_tags_gin_idx ON artifacts USING GIN (tags);
