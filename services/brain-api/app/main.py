"""KB Router — brain API (ingest + vault read/write + brain HTTP contract).

Endpoints:
  GET  /health                         — liveness
  GET  /health/deep                    — dependencies reachable (vault, embeddings, inference)
  GET  /health/pipeline                — the loop is flowing (ingest → … → index)
  POST /ingest                         — classify + fan out (operator message)
  POST /ingest_with_files              — same, with multipart attachments
  POST /index_artifact                 — embed + index an artifact (called by artifact-store)
  GET  /feed                           — hot arcs + inject blocks + intent; ?project= one project
  GET  /signal                         — current amygdala signal (single file)
  GET  /denials                        — cross scope query denials (router tokens only)
  POST /marker                         — emit lesson/milestone/signal/decision
  POST /graph/entity                   — create or revise a typed graph entity
  POST /graph/relation                 — store a typed relation (bad ones rejected or quarantined)
  GET  /graph/entity/{id}              — an entity and its relations inside the caller scope
  GET  /graph/stats                    — graph_invalid_relations_total and quarantine size
  POST /tick                           — request a manual agentibrain tick
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
import threading
import time
from typing import Annotated, Any

from fastapi import (
    Body,
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi import (
    Path as PathParam,
)
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from . import arcs, graph, pipeline, provenance, scope, vault_reader
from .feed import VAULT_ROOT, feed_payload, project_feed_payload
from .markers import (
    MarkerError,
    claimed_projects,
    granted_names,
    marker_id_for,
    read_marker,
    write_marker,
)
from .retrieval.graph import ExpansionRequest, expand
from .router import IngestResult, ingest_message
from .signal import read_signal
from .tick_trigger import enqueue_tick, get_tick_status

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
# Sync route handlers run in Starlette's thread pool, so concurrent POSTs
# (CLI sync racing tick-cron's drain on the same outbox) genuinely execute in
# parallel threads. The check-then-act on the cache must be atomic or both
# miss and double-write the marker.
_idempotency_lock = threading.Lock()
# This lock also serializes `write_marker` itself, not just the cache — the
# signal dedup inside it is a scan-then-write, which is only race-free because
# the whole thing happens under here. Across processes on one host it also
# holds, because write_marker takes the marker registry's flock in the vault;
# across hosts only as far as the vault mount honours flock.
_feed_cache: dict[str, Any] = {"ts": 0.0, "payload": None}


def _purge_idempotency() -> None:
    now = time.time()
    stale = [k for k, (ts, _) in _idempotency_cache.items() if (now - ts) > IDEMPOTENCY_TTL_SECONDS]
    for k in stale:
        _idempotency_cache.pop(k, None)


def _bearer(authorization: str | None) -> str:
    # Fail closed. An unconfigured token set used to mean "serve everything to
    # anyone", which is the worst possible reading of a missing credential:
    # the vault, the feed and the marker endpoints all answered unauthenticated.
    # init and install always generate a bearer, so an empty set means the
    # deployment is misconfigured, not that it wanted to be public.
    if not KB_ROUTER_TOKENS:
        raise HTTPException(
            status_code=503,
            detail=(
                "no KB_ROUTER_TOKEN configured — brain-api refuses to serve "
                "unauthenticated. Set KB_ROUTER_TOKEN (or KB_ROUTER_TOKENS) and restart."
            ),
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    return authorization.split(" ", 1)[1].strip()


def require_token(
    request: Request,
    authorization: str | None = Header(None),
    x_session_title: str | None = Header(None, alias="X-Session-Title"),
) -> None:
    token = _bearer(authorization)
    if token in KB_ROUTER_TOKENS:
        return
    brain = scope.worker_brain(token)
    if brain is None:
        raise HTTPException(status_code=401, detail="invalid token")
    if brain != scope.BRAIN_ID:
        scope.record_denial(brain, request.url.path, x_session_title or "")
        raise HTTPException(status_code=403, detail=f"token names brain {brain}, not this brain")


def require_router_token(
    authorization: str | None = Header(None), _: None = Depends(require_token)
) -> None:
    if _bearer(authorization) not in KB_ROUTER_TOKENS:
        raise HTTPException(status_code=403, detail="denials are served to router tokens only")


app = FastAPI(title="agentibrain brain-api", version="0.3.0")


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "service": "brain-api",
        "vault_root": str(VAULT_ROOT),
        "vault_mounted": VAULT_ROOT.exists(),
    }


@app.get("/health/deep")
async def health_deep(_: None = Depends(require_token)) -> dict:
    """End-to-end readiness across the whole kernel.

    Verifies what liveness /health cannot: the vault is actually writable,
    the embeddings service can reach both its DB and the embedding model
    (via its own /health/deep), and the inference gateway accepts our key.
    """
    import uuid

    import httpx

    checks: dict[str, Any] = {}
    ok = True

    # Vault: prove a real write round-trips, not just that the mount exists.
    # The probe name is unique per call so concurrent /health/deep requests
    # never write or unlink each other's file (a shared name races: the second
    # unlink hits a missing path and falsely reports the vault unwritable).
    probe = VAULT_ROOT / f".healthcheck.{os.getpid()}.{uuid.uuid4().hex}"
    try:
        probe.write_text(str(time.time()))
        checks["vault"] = {"ok": True, "root": str(VAULT_ROOT), "writable": True}
    except OSError as exc:
        ok = False
        checks["vault"] = {"ok": False, "root": str(VAULT_ROOT), "error": str(exc)[:300]}
    finally:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass

    async with httpx.AsyncClient(timeout=30) as client:
        # Embeddings: delegate to its deep check (DB + live embed + dim match).
        if not EMBEDDINGS_URL or not _EMBEDDINGS_API_KEY:
            ok = False
            checks["embeddings"] = {
                "ok": False,
                "error": "EMBEDDINGS_URL or EMBEDDINGS_API_KEY not configured",
            }
        else:
            try:
                resp = await client.get(
                    f"{EMBEDDINGS_URL.rstrip('/')}/health/deep",
                    headers={"Authorization": f"Bearer {_EMBEDDINGS_API_KEY}"},
                )
                body = resp.json() if resp.status_code == 200 else {"error": resp.text[:300]}
                healthy = resp.status_code == 200 and body.get("status") == "ok"
                if not healthy:
                    ok = False
                checks["embeddings"] = {"ok": healthy, "url": EMBEDDINGS_URL, **body}
            except Exception as exc:
                ok = False
                checks["embeddings"] = {"ok": False, "url": EMBEDDINGS_URL, "error": str(exc)[:300]}

        # Inference: the gateway answers /models with our key, the classify
        # model is in the catalogue, AND a real one-token completion round-
        # trips ("ping" → "pong"). The completion is the proof a listed model
        # actually serves — catalogue presence alone hides quota exhaustion
        # and broken deployments. Deep health is on-demand only (see the
        # embeddings docstring), so the spend is one tiny completion per call.
        inference_url = os.getenv("INFERENCE_URL", "")
        inference_key = os.getenv("INFERENCE_API_KEY", "")
        if not inference_url:
            ok = False
            checks["inference"] = {"ok": False, "error": "INFERENCE_URL not configured"}
        else:
            try:
                resp = await client.get(
                    f"{inference_url.rstrip('/')}/models",
                    headers={"Authorization": f"Bearer {inference_key}"},
                )
                healthy = resp.status_code == 200
                if not healthy:
                    ok = False
                detail: dict[str, Any] = {
                    "ok": healthy,
                    "url": inference_url,
                    "http_status": resp.status_code,
                }
                if healthy:
                    models = resp.json().get("data", [])
                    detail["model_count"] = len(models)
                    classify_model = os.getenv("BRAIN_CLASSIFY_MODEL", "")
                    if classify_model:
                        detail["classify_model"] = classify_model
                        # Informational only: wildcard/passthrough routing can
                        # serve models /models never enumerates, so absence
                        # from the catalogue must not degrade health. The
                        # completion below is the arbiter.
                        detail["classify_model_available"] = any(
                            m.get("id") == classify_model for m in models
                        )
                        try:
                            t0 = time.monotonic()
                            comp = await client.post(
                                f"{inference_url.rstrip('/')}/chat/completions",
                                headers={"Authorization": f"Bearer {inference_key}"},
                                json={
                                    "model": classify_model,
                                    "messages": [
                                        {
                                            "role": "user",
                                            "content": "Reply with exactly one word: pong",
                                        }
                                    ],
                                    "max_tokens": 5,
                                    "temperature": 0,
                                },
                            )
                            comp.raise_for_status()
                            text = (comp.json()["choices"][0]["message"]["content"] or "").strip()
                            pong = "pong" in text.lower()
                            if not pong:
                                ok = False
                                detail["ok"] = False
                            detail["completion"] = {
                                "ok": pong,
                                "probe": "ping",
                                "response": text[:80],
                                "latency_ms": round((time.monotonic() - t0) * 1000),
                            }
                        except Exception as exc:
                            ok = False
                            detail["ok"] = False
                            detail["completion"] = {
                                "ok": False,
                                "error": str(exc)[:300],
                            }
                else:
                    detail["error"] = resp.text[:300]
                checks["inference"] = detail
            except Exception as exc:
                ok = False
                checks["inference"] = {"ok": False, "url": inference_url, "error": str(exc)[:300]}

    return {"status": "ok" if ok else "degraded", "service": "brain-api", "checks": checks}


@app.get("/health/pipeline")
async def health_pipeline(_: None = Depends(require_token)) -> dict:
    """Is the brain's loop flowing — ingest → drain → arcs → lessons → signals → feed → index.

    Distinct from /health/deep, which asks whether dependencies are reachable.
    A stack can pass every dependency check and still be dead: the drain not
    consuming, every tick failing, lessons written but never fed back.

    Cheap by construction — bounded vault globs plus one grouped count from the
    embeddings service. No embedding call, no completion call. Runs server-side
    because the vault is only mounted here, which is what lets this answer for
    a remote deployment.
    """
    import httpx

    index_stats: dict | None = None
    if EMBEDDINGS_URL and _EMBEDDINGS_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{EMBEDDINGS_URL.rstrip('/')}/stats",
                    headers={"Authorization": f"Bearer {_EMBEDDINGS_API_KEY}"},
                )
            index_stats = resp.json() if resp.status_code == 200 else {"error": resp.text[:300]}
        except Exception as exc:  # noqa: BLE001 — an unreachable index degrades, never 500s
            index_stats = {"error": str(exc)[:300]}

    # pipeline_report is synchronous filesystem work against what is an NFS
    # mount in production. Called directly from this `async def` it would run
    # ON the event loop and stall every other request for the duration of the
    # walk — the handler must hand it to the threadpool the way FastAPI does
    # for a plain `def` endpoint.
    return await run_in_threadpool(pipeline.pipeline_report, index_stats=index_stats)


# ---------------------------------------------------------------------------
# Vault read/write — absorbed from the former obsidian-reader microservice
# ---------------------------------------------------------------------------


@app.get("/vault/list")
def vault_list(
    prefix: str = Query("", description="Directory prefix to list (relative to vault root)"),
    extensions: str = Query(
        ".md,.markdown,.txt", description="Comma-separated extensions to include"
    ),
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
    try:
        return vault_reader.search_vault(
            q=q, prefix=prefix, limit=limit, context_lines=context_lines
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))


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
            title=title,
            content=content,
            tags=tag_list,
            artifact_refs=ref_list,
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
    repository: str = Form("", description="Explicit repository to clone and ingest"),
    _: None = Depends(require_token),
) -> dict:
    result: IngestResult = await ingest_message(message, repository=repository)
    return result.to_dict()


@app.post("/ingest_with_files")
async def ingest_with_files(
    message: str = Form(...),
    files: list[UploadFile] = File(default=[]),
    repository: str = Form("", description="Explicit repository to clone and ingest"),
    _: None = Depends(require_token),
) -> dict:
    """Ingest with multipart files attached. Files are written to the vault inbox
    as text notes, then the message is classified and ingested normally.
    """
    from .router import _slugify

    pre_paths: list[str] = []
    errors: list[str] = []

    for f in files or []:
        content_bytes = await f.read()
        filename = f.filename or "upload.bin"
        slug = _slugify(filename.rsplit(".", 1)[0])
        try:
            text = content_bytes.decode("utf-8", errors="replace")
            result = vault_reader.write_inbox(
                title=slug,
                content=f"# Uploaded File: {filename}\n\n{text[:500_000]}",
                tags=["uploaded-file", "multipart"],
                artifact_refs=[],
            )
            if result.get("path"):
                pre_paths.append(result["path"])
        except Exception as exc:
            errors.append(f"multipart write failed: {filename} — {exc}")

    result: IngestResult = await ingest_message(message, repository=repository)
    result.vault_paths = pre_paths + result.vault_paths
    result.errors = errors + result.errors
    return result.to_dict()


# ── /index_artifact — sole brain-side write surface for artifact embeddings ──

EMBEDDINGS_URL = os.getenv("EMBEDDINGS_URL", "")
_EMBEDDINGS_API_KEY = os.environ.get("EMBEDDINGS_API_KEY") or ""


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
def feed(
    project: str | None = Query(None, min_length=1, max_length=512),
    authorization: str | None = Header(None),
    x_session_title: str | None = Header(None, alias="X-Session-Title"),
    _: None = Depends(require_token),
) -> dict:
    """Hot arcs + inject blocks + operator intent from $VAULT_ROOT/brain-feed/.

    With `project`, the feed is that project's own snapshot; a token granted
    other projects is refused, and a granted token must name its project.
    """
    grant = _caller_grant(authorization)
    if project is not None:
        if not scope.admits(grant, {project}, unattributed=False):
            scope.record_denial(
                scope.worker_brain(_bearer(authorization)) or "", "GET /feed", x_session_title or ""
            )
            raise HTTPException(status_code=403, detail="project is outside this token's grant")
        return project_feed_payload(project, scope.BRAIN_ID)
    if grant:
        raise HTTPException(status_code=400, detail="a project scoped token must name its project")
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


@app.get("/denials")
def denials(
    since_ms: int = Query(0, ge=0),
    _: None = Depends(require_router_token),
) -> dict:
    return {"brain_id": scope.BRAIN_ID, "denials": scope.denials_since(since_ms)}


def _hash_marker_body(marker_type: str, content: str, attrs: dict) -> str:
    raw = json.dumps(
        {"type": marker_type, "content": content, "attrs": attrs or {}},
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _caller_grant(authorization: str | None) -> frozenset[str]:
    token = _bearer(authorization)
    return frozenset() if token in KB_ROUTER_TOKENS else scope.project_grant(token)


@app.post("/marker", status_code=201)
def post_marker(
    payload: dict = Body(...),
    x_idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    authorization: str | None = Header(None),
    x_session_title: str | None = Header(None, alias="X-Session-Title"),
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

    A worker token with a project grant is refused a marker that claims a
    project outside it; a marker claiming no project stays unattributed.
    Only an explicit X-Idempotency-Key makes a marker id that outlives the
    cache window.
    """
    marker_type = (payload.get("type") or "").strip().lower()
    content = payload.get("content") or ""
    attrs = payload.get("attrs") or {}
    if not isinstance(attrs, dict):
        raise HTTPException(status_code=400, detail="attrs must be an object")
    grant = _caller_grant(authorization)
    named = str(attrs.get("project") or "").strip()
    foreign_name = bool(grant and named and named not in granted_names(grant))
    if foreign_name or not scope.admits(grant, claimed_projects(attrs), unattributed=True):
        scope.record_denial(
            scope.worker_brain(_bearer(authorization)) or "", "POST /marker", x_session_title or ""
        )
        raise HTTPException(status_code=403, detail="marker project is outside this token's grant")

    body_hash = _hash_marker_body(marker_type, content, attrs)
    explicit_key = (x_idempotency_key or "").strip()
    idem_key = explicit_key or body_hash

    # The lock spans check-write-record so two threads carrying the same key
    # cannot both miss the cache and double-apply; it also serialises the
    # non-atomic read-modify-write appends inside write_marker.
    with _idempotency_lock:
        _purge_idempotency()
        cached = _idempotency_cache.get(idem_key)
        if cached is not None:
            cached_payload = dict(cached[1])
            cached_payload["idempotent_replay"] = True
            return cached_payload

        try:
            result = write_marker(
                marker_type,
                content,
                attrs,
                marker_id=marker_id_for(explicit_key) if explicit_key else None,
                brain=scope.BRAIN_ID,
                grant=grant,
            )
        except MarkerError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except OSError as exc:
            log.error("marker write failed: %s", exc)
            raise HTTPException(status_code=503, detail=f"vault write failed: {exc}")

        if result["marker_provenance_loss_total"]:
            log.warning(
                "marker_provenance_loss_total=%d marker_type=%s marker_id=%s",
                result["marker_provenance_loss_total"],
                result["marker_type"],
                result["marker_id"],
            )
        response = {
            "ok": True,
            "idempotency_key": idem_key,
            "body_hash": body_hash,
            **result,
        }
        if result["action"] == "replayed":
            response["idempotent_replay"] = True
        _idempotency_cache[idem_key] = (time.time(), response)
        return response


@app.get("/marker/{marker_id}")
def get_marker(
    marker_id: str = PathParam(..., min_length=1, max_length=64),
    authorization: str | None = Header(None),
    _: None = Depends(require_token),
) -> dict:
    """A recorded marker with its provenance read back from the vault."""
    record = read_marker(marker_id)
    if record is None or not scope.admits(
        _caller_grant(authorization),
        claimed_projects(record["provenance"]),
        unattributed=False,
    ):
        raise HTTPException(status_code=404, detail="marker not found")
    return record


def _graph_store() -> graph.GraphStore:
    store = graph.active_store()
    if store is None:
        raise HTTPException(status_code=404, detail="knowledge graph is inactive")
    return store


def _graph_visible(authorization: str | None):
    grant = _caller_grant(authorization)
    return lambda entity: scope.admits(grant, set(entity["projects"]), unattributed=False)


@app.post("/graph/entity", status_code=201)
def post_graph_entity(
    payload: dict,
    authorization: str | None = Header(None),
    x_session_title: str | None = Header(None, alias="X-Session-Title"),
    _: None = Depends(require_token),
) -> dict:
    """Create or revise a graph entity; a stale or conflicting revision is a 409."""
    store = _graph_store()
    grant = _caller_grant(authorization)
    projects = payload.get("projects") if isinstance(payload.get("projects"), list) else []
    if not all(isinstance(p, str) for p in projects):
        raise HTTPException(
            status_code=422,
            detail={"reason": "invalid_entity", "error": "projects must be a list of project ids"},
        )
    stored = store.get_entity(payload["id"]) if isinstance(payload.get("id"), str) else None
    if not scope.admits(grant, set(projects), unattributed=False) or (
        stored is not None and not scope.admits(grant, set(stored["projects"]), unattributed=False)
    ):
        scope.record_denial(
            scope.worker_brain(_bearer(authorization)) or "",
            "POST /graph/entity",
            x_session_title or "",
        )
        raise HTTPException(status_code=403, detail="entity project is outside this token's grant")
    try:
        action = store.put_entity(payload)
    except graph.GraphRejection as exc:
        raise HTTPException(
            status_code=422, detail={"reason": exc.reason, "error": str(exc)}
        ) from exc
    if action == "stale":
        raise HTTPException(status_code=409, detail="a newer or different revision is stored")
    return {"ok": True, "action": action, "id": payload["id"]}


@app.post("/graph/relation", status_code=201)
def post_graph_relation(
    payload: dict,
    quarantine: bool = Query(False),
    authorization: str | None = Header(None),
    _: None = Depends(require_token),
) -> Any:
    """Store a typed relation; a bad one is a 422, or a 202 when quarantine is asked for."""
    store = _graph_store()
    try:
        result = store.put_relation(
            payload, quarantine=quarantine, visible=_graph_visible(authorization)
        )
    except graph.GraphRejection as exc:
        total = sum(store.invalid_relations_total().values())
        log.warning("graph_invalid_relations_total=%d reason=%s", total, exc.reason)
        raise HTTPException(
            status_code=422,
            detail={"reason": exc.reason, "graph_invalid_relations_total": total},
        ) from exc
    if result["action"] == "stale":
        raise HTTPException(status_code=409, detail="a newer or different revision is stored")
    if result["action"] == "quarantined":
        total = sum(store.invalid_relations_total().values())
        log.warning("graph_invalid_relations_total=%d reason=%s", total, result["reason"])
        return JSONResponse(
            status_code=202, content={**result, "graph_invalid_relations_total": total}
        )
    return {"ok": True, **result}


@app.get("/graph/entity/{entity_id:path}")
def get_graph_entity(
    entity_id: str = PathParam(..., min_length=3, max_length=256),
    authorization: str | None = Header(None),
    _: None = Depends(require_token),
) -> dict:
    """An entity with every relation whose other end the caller may read."""
    store = _graph_store()
    visible = _graph_visible(authorization)
    entity = store.get_entity(entity_id)
    if entity is None or not visible(entity):
        raise HTTPException(status_code=404, detail="entity not found")
    return {"entity": entity, "relations": store.relations(entity_id, visible)}


@app.post("/graph/expand")
def post_graph_expand(
    body: ExpansionRequest,
    authorization: str | None = Header(None),
    _: None = Depends(require_token),
) -> dict:
    return expand(_graph_store(), body, _caller_grant(authorization), _bearer(authorization))


@app.post("/knowledge/detail", status_code=201)
def publish_knowledge_detail(
    payload: arcs.Publication,
    _: None = Depends(require_router_token),
) -> dict:
    store = _graph_store()
    try:
        action, value = store.put_detail(
            payload.model_dump(), lambda entity: arcs.document(payload, entity)
        )
    except graph.GraphRejection as exc:
        raise HTTPException(status_code=409, detail={"reason": exc.reason}) from exc
    return {"action": action, "content_hash": value["content_hash"], "revision": payload.revision}


@app.get("/knowledge/neighbors")
def read_knowledge_neighbors(
    entity_id: str = Query(..., min_length=3, max_length=256),
    after: str = Query("", max_length=256),
    limit: int = Query(25, ge=1, le=100),
    authorization: str | None = Header(None),
    _: None = Depends(require_token),
) -> dict:
    result = _graph_store().neighbor_page(entity_id, after, limit, _graph_visible(authorization))
    if result is None:
        raise HTTPException(status_code=404, detail="entity not found")
    return result


@app.get("/knowledge/detail")
def read_knowledge_detail(
    query: Annotated[arcs.ReadRequest, Depends(arcs.ReadRequest)],
    authorization: str | None = Header(None),
    _: None = Depends(require_token),
) -> dict:
    if os.getenv("BRAIN_FULL_DETAIL", "true").lower() == "false":
        raise HTTPException(status_code=503, detail="full detail reads are disabled")
    store = _graph_store()
    visible = _graph_visible(authorization)
    value = store.read_detail(query.entity_id, query.revision, visible)
    if value is None:
        raise HTTPException(status_code=404, detail="detail revision unavailable")
    if query.content_hash and query.content_hash != value["content_hash"]:
        raise HTTPException(status_code=409, detail="detail hash does not match reference")
    if (query.section_offset or query.reference_offset) and query.revision is None:
        raise HTTPException(status_code=422, detail="continuations require an explicit revision")
    return {
        "entity_id": query.entity_id,
        "revision": value["entity"]["revision"],
        "content_hash": value["content_hash"],
        "total_bytes": value["total_bytes"],
        "trust_class": "original_evidence"
        if value["entity"]["type"] == "evidence"
        else "synthesized_knowledge",
        "provenance": value["entity"]["provenance"],
        **arcs.page(value, query.section, query.section_offset, query.section_limit),
        **provenance.page(
            store, value["references"], visible, query.reference_offset, query.reference_limit
        ),
    }


@app.get("/graph/stats")
def get_graph_stats(_: None = Depends(require_router_token)) -> dict:
    """Invalid relation counts by reason and the quarantine size (router tokens only)."""
    store = _graph_store()
    return {
        "graph_invalid_relations_total": store.invalid_relations_total(),
        "quarantined": len(store.quarantined()),
    }


@app.post("/tick", status_code=202)
def post_tick(
    dry_run: bool = Query(False),
    no_ai: bool = Query(False),
    source: str = Query("brain-api"),
    _: None = Depends(require_token),
) -> dict:
    """Request a manual agentibrain tick. Returns 202 with a job_id.

    Writes a request file to brain-feed/ticks/requested/. The tick-engine
    CronJob picks this up and moves it to completed/ or failed/ when done.
    Redundant same-kind requests are coalesced by the drain, not here.
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
