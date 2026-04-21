"""Anton Embeddings — centralized embedding service for semantic search."""

import logging
import os
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException
from pydantic import BaseModel

import auth
import db
import embed

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("anton-embeddings")

app = FastAPI(title="Anton Embeddings", version="1.0.0")


class EmbedRequest(BaseModel):
    key: str
    content: str
    producer: str = "unknown"
    content_type: str = "text/plain"
    metadata: dict = {}


class SearchRequest(BaseModel):
    query: str
    producer: Optional[str] = None
    limit: int = 10
    min_score: float = 0.0


@app.on_event("startup")
def startup():
    try:
        db.get_pool()
        log.info("db_connected")
    except Exception as e:
        log.warning(f"db_init_deferred error={e}")

    if embed.is_configured():
        log.info(f"embedding_model={embed.LLM_EMBED_MODEL}")
    else:
        log.warning("embedding_not_configured — LLM_API_BASE or LLM_API_KEY missing")

    log.info("anton_embeddings_ready")


@app.get("/health")
def health():
    result = {"status": "ok", "embedding_model": embed.LLM_EMBED_MODEL}
    try:
        result["vector_count"] = db.get_vector_count()
    except Exception:
        result["vector_count"] = -1
    result["embedding_configured"] = embed.is_configured()
    return result


@app.post("/embed")
async def embed_content(
    req: EmbedRequest,
    _token: str = Depends(auth.require_api_key),
):
    if not embed.is_configured():
        raise HTTPException(503, "Embedding model not configured")

    try:
        chunks = embed.embed_content(req.content)
    except Exception as e:
        log.error(f"embed_failed key={req.key} error={e}")
        raise HTTPException(500, f"Embedding failed: {e}")

    for chunk in chunks:
        chunk["metadata"] = req.metadata

    try:
        stored = db.upsert_chunks(
            key=req.key,
            producer=req.producer,
            content_type=req.content_type,
            chunks=chunks,
        )
    except Exception as e:
        log.error(f"store_failed key={req.key} error={e}")
        raise HTTPException(500, f"Storage failed: {e}")

    log.info(f"embedded key={req.key} chunks={stored} dims={len(chunks[0]['embedding'])}")
    return {
        "key": req.key,
        "chunks_stored": stored,
        "dimensions": len(chunks[0]["embedding"]),
    }


@app.post("/search")
async def search_content(
    req: SearchRequest,
    _token: str = Depends(auth.require_api_key),
):
    if not embed.is_configured():
        raise HTTPException(503, "Embedding model not configured")

    try:
        query_vec = embed.embed_text(req.query)
    except Exception as e:
        log.error(f"query_embed_failed error={e}")
        raise HTTPException(500, f"Query embedding failed: {e}")

    results = db.search(
        query_embedding=query_vec,
        producer=req.producer,
        limit=req.limit,
        min_score=req.min_score,
    )

    return {"query": req.query, "results": results, "count": len(results)}
