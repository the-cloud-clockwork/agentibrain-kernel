"""Postgres + pgvector storage for content embeddings."""

import os
import logging

from psycopg_pool import ConnectionPool
from psycopg.types.json import Jsonb

log = logging.getLogger("anton-embeddings.db")

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
    init_sql = os.path.join(os.path.dirname(__file__), "init.sql")
    if os.path.exists(init_sql):
        with pool.connection() as conn:
            with open(init_sql) as f:
                conn.execute(f.read())
            conn.commit()
        log.info("schema_initialized")


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


def get_vector_count() -> int:
    pool = get_pool()
    with pool.connection() as conn:
        cur = conn.execute("SELECT COUNT(*) FROM content_embeddings")
        return cur.fetchone()[0]
