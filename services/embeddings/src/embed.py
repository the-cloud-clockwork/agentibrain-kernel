"""Chunking + embedding via OpenAI-compatible API (LiteLLM)."""

import logging
import os

import httpx

log = logging.getLogger("agentibrain-embeddings.embed")

LLM_API_BASE = os.environ.get("LLM_API_BASE", "")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_EMBED_MODEL = os.environ.get("LLM_EMBED_MODEL", "text-embedding-3-small")

MAX_CHUNK_CHARS = 4000

# Known output dimensions per model family. The schema column must match the
# model or every insert fails with "expected N dimensions".
_MODEL_DIMS = {
    "text-embedding-3-large": 3072,
    "text-embedding-3-small": 1536,
    "text-embedding-ada-002": 1536,
}


class UnknownEmbedDimError(RuntimeError):
    """LLM_EMBED_MODEL is not a known family and EMBED_DIM is unset.

    Guessing a dimension here is unsafe: a wrong guess sizes the pgvector
    column wrong, which either fails every insert or (on a populated table)
    invites a destructive migration. The operator must pin EMBED_DIM.
    """


def target_dim() -> int:
    """Vector dimension the schema must hold.

    Resolution order: explicit EMBED_DIM env wins; otherwise inferred from
    LLM_EMBED_MODEL. Matching is case-insensitive, ignores provider prefixes
    (`azure/…`), and prefers the longest family name so a short key cannot
    shadow a more specific one. An unrecognised model raises rather than
    guessing — see UnknownEmbedDimError.
    """
    explicit = os.environ.get("EMBED_DIM", "")
    if explicit:
        return int(explicit)
    model = LLM_EMBED_MODEL.rsplit("/", 1)[-1].lower()
    for name in sorted(_MODEL_DIMS, key=len, reverse=True):
        if name in model:
            return _MODEL_DIMS[name]
    raise UnknownEmbedDimError(
        f"cannot infer embedding dimension for LLM_EMBED_MODEL={LLM_EMBED_MODEL!r}; "
        f"known families={sorted(_MODEL_DIMS)}. Set EMBED_DIM to pin it explicitly."
    )


def is_configured() -> bool:
    return bool(LLM_API_BASE) and bool(LLM_API_KEY)


def embed_text(text: str, model: str | None = None) -> list[float]:
    """Generate an embedding vector via POST {base}/embeddings."""
    if not LLM_API_BASE or not LLM_API_KEY:
        raise RuntimeError("LLM_API_BASE and LLM_API_KEY must be set")

    model = model or LLM_EMBED_MODEL
    resp = httpx.post(
        f"{LLM_API_BASE.rstrip('/')}/embeddings",
        headers={
            "Authorization": f"Bearer {LLM_API_KEY}",
            "Content-Type": "application/json",
        },
        json={"model": model, "input": text},
        timeout=30.0,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["data"][0]["embedding"]


def chunk_content(content: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """Split content into chunks by paragraph boundaries."""
    if len(content) <= max_chars:
        return [content]

    chunks = []
    paragraphs = content.split("\n\n")
    current = ""

    for para in paragraphs:
        if len(current) + len(para) + 2 > max_chars:
            if current.strip():
                chunks.append(current.strip())
            current = para
        else:
            current = current + "\n\n" + para if current else para

    if current.strip():
        chunks.append(current.strip())

    return chunks if chunks else [content[:max_chars]]


def embed_content(content: str) -> list[dict]:
    """Chunk content and generate embeddings for each chunk."""
    chunks = chunk_content(content)
    results = []

    for idx, chunk_text in enumerate(chunks):
        try:
            embedding = embed_text(chunk_text)
            results.append({
                "chunk_idx": idx,
                "text_preview": chunk_text[:500],
                "embedding": embedding,
            })
        except Exception as e:
            log.error(f"embed_chunk_failed chunk={idx} error={e}")
            raise

    return results
