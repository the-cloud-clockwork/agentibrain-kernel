"""KB Router — brain API (ingest + vault read/write + brain HTTP contract).

Endpoints:
  GET  /health                         — liveness
  POST /ingest                         — classify + fan out (operator message)
  POST /ingest_with_files              — same, with multipart attachments
  POST /index_artifact                 — embed + index an artifact (called by artifact-store)
  GET  /feed                           — hot arcs + inject blocks + intent
  GET  /signal                         — current amygdala signal (single file)
  POST /marker                         — emit lesson/milestone/signal/decision
  POST /tick                           — request a manual brain tick
  GET  /tick/{job_id}                  — look up tick status
  GET  /vault/list                     — list vault files
  GET  /vault/read                     — read a single vault file
  GET  /vault/search                   — substring search across vault text files
  POST /vault/write_inbox              — write a note to raw/inbox/

Bearer auth via KB_ROUTER_TOKENS (plural, comma-sep) or KB_ROUTER_TOKEN (singular).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from fastapi import (
    Body,
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Path as PathParam,
    Query,
    UploadFile,
)

from .feed import VAULT_ROOT, feed_payload
from .markers import MarkerError, write_marker
from .router import IngestResult, ingest_message
from .signal import read_signal
from .tick_trigger import enqueue_tick, get_tick_status
from . import vault_reader

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("brain-api.main")


def _load_tokens() -> list[str]:
    """Accept KB_ROUTER_TOKENS (plural) or KB_ROUTER_TOKEN (singular)."""
    raw = os.getenv("KB_ROUTER_TOKENS", "") or os.getenv("KB_ROUTER_TOKEN", "")
    return [t.strip() for t in raw.split(",") if t.strip()]


KB_ROUTER_TOKENS = _load_tokens()
IDEMPOTENCY_TTL_SECONDS = int(os.getenv("IDEMPOTENCY_TTL_SECONDS", "3600"))
_FEED_CACHE_TTL = int(os.getenv("FEED_CACHE_TTL_SECONDS", "30"))

_idempotency_cache: dict[str, tuple[float, dict]] = {}
_feed_cache: dict[str, Any] = {"ts": 0.0, "payload": None}


def _purge_idempotency() -> None:
    now = time.time()
    stale = [k for k, (ts, _) in _idempotency_cache.items() if (now - ts) > IDEMPOTENCY_TTL_SECONDS]
    for k in stale:
        _idempotency_cache.pop(k, None)


def require_token(authorization: str | None = Header(None)) -> None:
    if not KB_ROUTER_TOKENS:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    if token not in KB_ROUTER_TOKENS:
        raise HTTPException(status_code=401, detail="invalid token")


app = FastAPI(title="agentibrain brain-api", version="0.3.0")


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "service": "brain-api",
        "vault_root": str(VAULT_ROOT),
        "vault_mounted": VAULT_ROOT.exists(),
    }


# ---------------------------------------------------------------------------
# Vault read/write — absorbed from the former obsidian-reader microservice
# ---------------------------------------------------------------------------


@app.get("/vault/list")
def vault_list(
    prefix: str = Query("", description="Directory prefix to list (relative to vault root)"),
    extensions: str = Query(".md,.markdown,.txt", description="Comma-separated extensions to include"),
    limit: int = Query(500, ge=1, le=5000),
    _: None = Depends(require_token),
) -> dict:
    try:
        return vault_reader.list_files(prefix=prefix, extensions=extensions, limit=limit)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/vault/read")
def vault_read(
    path: str = Query(..., description="Relative path inside vault"),
    max_bytes: int = Query(vault_reader.MAX_FILE_BYTES, ge=1, le=vault_reader.MAX_FILE_BYTES),
    _: None = Depends(require_token),
) -> dict:
    try:
        return vault_reader.read_file(path, max_bytes=max_bytes)
    except FileNotFoundError:
        raise HTTPException(404, "not found")
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/vault/search")
def vault_search(
    q: str = Query(..., min_length=1, description="Search query (substring, case-insensitive)"),
    prefix: str = Query("", description="Limit to a subdirectory"),
    limit: int = Query(20, ge=1, le=200),
    context_lines: int = Query(2, ge=0, le=10),
    _: None = Depends(require_token),
) -> dict:
    return vault_reader.search_vault(q=q, prefix=prefix, limit=limit, context_lines=context_lines)


@app.post("/vault/write_inbox")
def vault_write_inbox(
    title: str = Form(..., max_length=200),
    content: str = Form(..., max_length=200_000),
    tags: str = Form("", description="Comma-separated tags for YAML frontmatter"),
    artifact_refs: str = Form("", description="Comma-separated artifact keys to cross-reference"),
    _: None = Depends(require_token),
) -> dict:
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    ref_list = [r.strip() for r in artifact_refs.split(",") if r.strip()]
    try:
        return vault_reader.write_inbox(
            title=title, content=content, tags=tag_list, artifact_refs=ref_list,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except OSError as exc:
        raise HTTPException(503, f"vault write failed: {exc}")


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


@app.post("/ingest")
async def ingest(
    message: str = Form(..., description="Operator message to classify + ingest"),
    _: None = Depends(require_token),
) -> dict:
    """Pure-text ingest: classify + fan out URLs/repos/local_paths, write Obsidian note."""
    result: IngestResult = await ingest_message(message)
    return result.to_dict()


@app.post("/ingest_with_files")
async def ingest_with_files(
    message: str = Form(...),
    files: list[UploadFile] = File(default=[]),
    _: None = Depends(require_token),
) -> dict:
    """Ingest with multipart files attached. Each file is uploaded to artifact-store
    as an ingest artifact BEFORE classification, then included in the Obsidian note's
    artifact_refs.
    """
    import httpx
    from .router import _upload_bytes_to_artifact_store, _slugify, ARTIFACT_STORE_URL
    from uuid import uuid4
    batch_id = uuid4().hex[:12]
    pre_keys: list[str] = []
    errors: list[str] = []

    async with httpx.AsyncClient(timeout=120) as client:
        for f in files or []:
            content = await f.read()
            filename = f.filename or "upload.bin"
            ct = f.content_type or "application/octet-stream"
            stem = filename.rsplit(".", 1)[0]
            ext = filename.rsplit(".", 1)[-1] if "." in filename else "bin"
            slug = _slugify(stem)
            try:
                r = await _upload_bytes_to_artifact_store(
                    content,
                    filename=filename,
                    content_type=ct,
                    producer="ingest",
                    artifact_type=ext,
                    slug=slug,
                    tags={"source": "multipart", "ingest_batch": batch_id, "original_filename": filename},
                    client=client,
                )
                if r.get("key"):
                    pre_keys.append(r["key"])
            except Exception as exc:
                errors.append(f"multipart upload failed: {filename} — {exc}")

    result: IngestResult = await ingest_message(message)
    result.artifact_keys = pre_keys + result.artifact_keys
    result.errors = errors + result.errors
    payload = result.to_dict()
    payload["multipart_keys"] = pre_keys
    return payload


# ── /index_artifact — sole brain-side write surface for artifact embeddings ──

EMBEDDINGS_URL = os.getenv("EMBEDDINGS_URL", "")
_EMBEDDINGS_API_KEY = (
    os.environ.get("EMBEDDINGS_API_KEY") or ""
)


@app.post("/index_artifact")
async def index_artifact(
    payload: dict = Body(...),
    _: None = Depends(require_token),
) -> dict:
    """Embed and index an artifact. Caller is artifact-store after a PUT.

    Body: {key, content, content_type, producer, metadata?}.
    Proxies to agentibrain-embeddings /embed with the kernel's API key.
    """
    if not EMBEDDINGS_URL or not _EMBEDDINGS_API_KEY:
        raise HTTPException(503, "EMBEDDINGS_URL or EMBEDDINGS_API_KEY not configured")

    required = ("key", "content")
    missing = [f for f in required if not payload.get(f)]
    if missing:
        raise HTTPException(400, f"missing required fields: {missing}")

    body = {
        "key": payload["key"],
        "content": payload["content"],
        "content_type": payload.get("content_type", "text/plain"),
        "producer": payload.get("producer", "unknown"),
        "metadata": payload.get("metadata") or {},
    }

    import httpx
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{EMBEDDINGS_URL.rstrip('/')}/embed",
                json=body,
                headers={
                    "Authorization": f"Bearer {_EMBEDDINGS_API_KEY}",
                    "Content-Type": "application/json",
                },
            )
            if not (200 <= resp.status_code < 300):
                detail = resp.text[:300]
                raise HTTPException(resp.status_code, f"embeddings: {detail}")
            return resp.json()
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"embeddings unreachable: {exc}")


# ---------------------------------------------------------------------------
# Brain HTTP contract — /feed /signal /marker /tick
# ---------------------------------------------------------------------------


@app.get("/feed")
def feed(_: None = Depends(require_token)) -> dict:
    """Hot arcs + inject blocks + operator intent from $VAULT_ROOT/brain-feed/."""
    now = time.time()
    cached = _feed_cache.get("payload")
    ts = float(_feed_cache.get("ts") or 0.0)
    if cached is not None and (now - ts) < _FEED_CACHE_TTL:
        return cached
    payload = feed_payload()
    _feed_cache["ts"] = now
    _feed_cache["payload"] = payload
    return payload


@app.get("/signal")
def signal(_: None = Depends(require_token)) -> dict:
    """Current amygdala signal. Absent file -> active=false, severity=null."""
    return read_signal()


def _hash_marker_body(marker_type: str, content: str, attrs: dict) -> str:
    raw = json.dumps(
        {"type": marker_type, "content": content, "attrs": attrs or {}},
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@app.post("/marker", status_code=201)
def post_marker(
    payload: dict = Body(...),
    x_idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    _: None = Depends(require_token),
) -> dict:
    """Emit a brain marker.

    Body:
      {
        "type": "lesson"|"milestone"|"signal"|"decision",
        "content": str (<=4096 chars),
        "attrs": {"severity": str?, "source": str?, "session_id": str?, ...}
      }
    Headers:
      X-Idempotency-Key: optional -- repeat calls with the same key return the
      original response without re-writing. Falls back to a content-hash key
      if absent.
    """
    marker_type = (payload.get("type") or "").strip().lower()
    content = payload.get("content") or ""
    attrs = payload.get("attrs") or {}
    if not isinstance(attrs, dict):
        raise HTTPException(status_code=400, detail="attrs must be an object")

    body_hash = _hash_marker_body(marker_type, content, attrs)
    idem_key = (x_idempotency_key or body_hash).strip()

    _purge_idempotency()
    cached = _idempotency_cache.get(idem_key)
    if cached is not None:
        cached_payload = dict(cached[1])
        cached_payload["idempotent_replay"] = True
        return cached_payload

    try:
        result = write_marker(marker_type, content, attrs)
    except MarkerError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except OSError as exc:
        log.error("marker write failed: %s", exc)
        raise HTTPException(status_code=503, detail=f"vault write failed: {exc}")

    response = {
        "ok": True,
        "idempotency_key": idem_key,
        "body_hash": body_hash,
        **result,
    }
    _idempotency_cache[idem_key] = (time.time(), response)
    return response


@app.post("/tick", status_code=202)
def post_tick(
    dry_run: bool = Query(False),
    no_ai: bool = Query(False),
    source: str = Query("brain-api"),
    _: None = Depends(require_token),
) -> dict:
    """Request a manual brain tick. Returns 202 with a job_id.

    Writes a request file to brain-feed/ticks/requested/. The tick-engine
    CronJob picks this up and moves it to completed/ or failed/ when done.
    Clients poll GET /tick/{job_id} for status.
    """
    try:
        return enqueue_tick(dry_run=dry_run, no_ai=no_ai, source=source)
    except OSError as exc:
        log.error("tick enqueue failed: %s", exc)
        raise HTTPException(status_code=503, detail=f"vault write failed: {exc}")


@app.get("/tick/{job_id}")
def get_tick(
    job_id: str = PathParam(..., min_length=1, max_length=64),
    _: None = Depends(require_token),
) -> dict:
    """Look up a tick job by id."""
    return get_tick_status(job_id)
