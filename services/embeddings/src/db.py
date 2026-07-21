"""Postgres + pgvector storage for content embeddings."""

import os
import logging

from psycopg_pool import ConnectionPool
from psycopg.types.json import Jsonb

import embed

log = logging.getLogger("agentibrain-embeddings.db")

HNSW_MAX_DIM = 2000  # pgvector HNSW indexes reject vectors wider than this

POSTGRES_URL = os.environ.get("POSTGRES_URL", "")

_pool = None


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        url = POSTGRES_URL
        if not url:
            raise RuntimeError("POSTGRES_URL not set")
        _pool = ConnectionPool(url, min_size=1, max_size=4, open=True)
        _ensure_schema(_pool)
    return _pool


def _ensure_schema(pool: ConnectionPool):
    dim = embed.target_dim()
    init_sql = os.path.join(os.path.dirname(__file__), "init.sql")
    if os.path.exists(init_sql):
        with pool.connection() as conn:
            with open(init_sql) as f:
                conn.execute(f.read().replace("{{EMBED_DIM}}", str(dim)))
            conn.commit()
        log.info(f"schema_initialized dim={dim}")
    _reconcile_dim(pool, dim)


def get_schema_dim(pool: ConnectionPool | None = None) -> int | None:
    """Dimension of the embedding column as it exists in the database."""
    pool = pool or get_pool()
    with pool.connection() as conn:
        cur = conn.execute(
            """SELECT atttypmod FROM pg_attribute
               WHERE attrelid = 'content_embeddings'::regclass
                 AND attname = 'embedding'"""
        )
        row = cur.fetchone()
        return row[0] if row else None


def _reconcile_dim(pool: ConnectionPool, dim: int):
    """Align the embedding column with the configured model dimension.

    A pre-existing table created for a different model (e.g. vector(1536) vs
    text-embedding-3-large's 3072) makes every insert fail. When the table is
    empty the column is altered in place; when it holds rows they belong to
    the old model and are useless against new-model query vectors, so they
    are dropped and re-embedded by the next embed_arcs pass.
    """
    current = get_schema_dim(pool)
    with pool.connection() as conn:
        if current is not None and current != dim:
            cur = conn.execute("SELECT COUNT(*) FROM content_embeddings")
            rows = cur.fetchone()[0]
            if rows:
                log.warning(
                    f"dim_migration_dropping_rows old_dim={current} new_dim={dim} rows={rows}"
                )
                conn.execute("DELETE FROM content_embeddings")
            conn.execute("DROP INDEX IF EXISTS idx_ce_embedding_hnsw")
            conn.execute(
                f"ALTER TABLE content_embeddings ALTER COLUMN embedding TYPE vector({dim})"
            )
            log.info(f"dim_migrated old={current} new={dim}")
        if dim <= HNSW_MAX_DIM:
            conn.execute(
                """CREATE INDEX IF NOT EXISTS idx_ce_embedding_hnsw
                   ON content_embeddings USING hnsw (embedding vector_cosine_ops)
                   WITH (m = 16, ef_construction = 64)"""
            )
        else:
            log.info(f"hnsw_skipped dim={dim} exceeds pgvector HNSW limit ({HNSW_MAX_DIM})")
        conn.commit()


def upsert_chunks(
    key: str,
    producer: str,
    content_type: str,
    chunks: list[dict],
):
    pool = get_pool()
    with pool.connection() as conn:
        # Delete existing chunks for this key (re-embed case)
        conn.execute("DELETE FROM content_embeddings WHERE key = %s", (key,))
        for chunk in chunks:
            conn.execute(
                """INSERT INTO content_embeddings
                   (key, chunk_idx, producer, content_type, text_preview, metadata, embedding)
                   VALUES (%s, %s, %s, %s, %s, %s, %s::vector)""",
                (
                    key,
                    chunk["chunk_idx"],
                    producer,
                    content_type,
                    chunk["text_preview"][:1000],
                    Jsonb(chunk.get("metadata", {})),
                    str(chunk["embedding"]),
                ),
            )
        conn.commit()
    return len(chunks)


def search(
    query_embedding: list[float],
    producer: str | None = None,
    limit: int = 10,
    min_score: float = 0.0,
) -> list[dict]:
    pool = get_pool()
    with pool.connection() as conn:
        conditions = ["1=1"]
        params: list = []

        if producer:
            conditions.append("producer = %s")
            params.append(producer)

        where = " AND ".join(conditions)
        vec_str = str(query_embedding)

        cur = conn.execute(
            f"""SELECT key, producer, chunk_idx, content_type, text_preview, metadata,
                       1 - (embedding <=> %s::vector) AS score
                FROM content_embeddings
                WHERE {where}
                ORDER BY embedding <=> %s::vector
                LIMIT %s""",
            [vec_str] + params + [vec_str, limit],
        )
        rows = cur.fetchall()
        results = []
        for row in rows:
            score = float(row[6])
            if score < min_score:
                continue
            results.append({
                "key": row[0],
                "producer": row[1],
                "chunk_idx": row[2],
                "content_type": row[3],
                "text_preview": row[4],
                "metadata": row[5],
                "score": round(score, 4),
            })
        return results


def prune(producer: str, keep_keys: list[str]) -> dict:
    """Delete all rows for `producer` whose `key` is not in `keep_keys`.

    Returns {deleted: N, kept: M, scanned: N+M}. Used by reaper jobs to
    clean orphan rows when source files (e.g. arc files) get renamed,
    graduated, or deleted from the vault.
    """
    pool = get_pool()
    with pool.connection() as conn:
        # Distinct key set for the producer.
        cur = conn.execute(
            "SELECT DISTINCT key FROM content_embeddings WHERE producer = %s",
            (producer,),
        )
        existing = {row[0] for row in cur.fetchall()}
        keep = set(keep_keys)
        to_delete = sorted(existing - keep)
        if to_delete:
            conn.execute(
                "DELETE FROM content_embeddings WHERE producer = %s AND key = ANY(%s)",
                (producer, list(to_delete)),
            )
            conn.commit()
        return {
            "deleted": len(to_delete),
            "kept": len(existing & keep),
            "scanned": len(existing),
            "deleted_keys": to_delete,
        }


def get_by_key(key: str) -> list[dict]:
    pool = get_pool()
    with pool.connection() as conn:
        cur = conn.execute(
            """SELECT key, producer, chunk_idx, content_type, text_preview, metadata
               FROM content_embeddings
               WHERE key = %s
               ORDER BY chunk_idx""",
            (key,),
        )
        return [
            {
                "key": row[0],
                "producer": row[1],
                "chunk_idx": row[2],
                "content_type": row[3],
                "text_preview": row[4],
                "metadata": row[5],
            }
            for row in cur.fetchall()
        ]


def get_vector_count() -> int:
    pool = get_pool()
    with pool.connection() as conn:
        cur = conn.execute("SELECT COUNT(*) FROM content_embeddings")
        return cur.fetchone()[0]
