"""KB Router — single-endpoint FastAPI ingest service.

POST /ingest accepts an operator message and (optional) multipart files, classifies
via Haiku, fans out to artifact-store + obsidian-reader, returns the batch result.
"""

from __future__ import annotations

import json
import logging
import os

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile

from .router import ingest_message, IngestResult

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("kb-router.main")

KB_ROUTER_TOKENS = [t.strip() for t in os.getenv("KB_ROUTER_TOKENS", "").split(",") if t.strip()]


def require_token(authorization: str | None = Header(None)) -> None:
    if not KB_ROUTER_TOKENS:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    if token not in KB_ROUTER_TOKENS:
        raise HTTPException(status_code=401, detail="invalid token")


app = FastAPI(title="Anton KB Router", version="0.1.0")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "kb-router"}


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

    # Now run classification on the message — it can reference URLs/repos/paths independently.
    result: IngestResult = await ingest_message(message)
    # Merge pre-uploaded multipart refs into the result
    result.artifact_keys = pre_keys + result.artifact_keys
    result.errors = errors + result.errors
    payload = result.to_dict()
    payload["multipart_keys"] = pre_keys
    return payload
