-- agentibrain-kernel — Postgres init.
-- Mounted into pgvector/pgvector:pg16 at /docker-entrypoint-initdb.d/.
-- Runs once on first container boot (skipped on subsequent boots — Postgres
-- only runs init scripts when the data dir is empty).
--
-- The embeddings service auto-applies its own DDL via _ensure_schema() on
-- startup, so this file only needs to install the pgvector extension. The
-- service's own migrations create the tables and indexes.
CREATE EXTENSION IF NOT EXISTS vector;
