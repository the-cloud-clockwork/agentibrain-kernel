"""Ingest router — one LLM call (Haiku) to classify the operator message, then deterministic fan-out.

Flow:
  1. LLM extracts: semantic_text, extractables (urls/repos/local_paths/multipart_refs), title, tags
  2. Semantic text → write Obsidian note (via obsidian-reader /write_inbox)
  3. Each extractable → fetch/clone/read → PUT to artifact-store under raw/ingest/<batch>/
  4. Cross-link tags: {obsidian_ref, ingest_batch} on every artifact; artifact_refs in Obsidian frontmatter
  5. Return bundle of what was created
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx

log = logging.getLogger("kb_router")

# ARTIFACT_STORE_URL is optional. When unset, binary-blob ingest raises a
# clear error rather than silently hitting a non-existent DNS name. Operators
# point this at their storage plane (an upstream artifact-store, an S3 gateway,
# or any OpenAPI-compatible blob service).
ARTIFACT_STORE_URL = os.getenv("ARTIFACT_STORE_URL", "")
ARTIFACT_STORE_KEY = os.getenv("ARTIFACT_STORE_KEY", "")
OBSIDIAN_READER_URL = os.getenv("OBSIDIAN_READER_URL", "http://obsidian-reader:8080")
OBSIDIAN_READER_TOKEN = os.getenv("OBSIDIAN_READER_TOKEN", "")

INFERENCE_URL = os.getenv("INFERENCE_URL", "")
INFERENCE_TOKEN_ENV = "INFERENCE_API_KEY"
BRAIN_CLASSIFY_MODEL = os.getenv("BRAIN_CLASSIFY_MODEL", "brain-classify")

# Comma-separated allowlist of filesystem roots the router is permitted to read from.
LOCAL_READ_ROOTS = [
    Path(p.strip()).resolve()
    for p in os.getenv("LOCAL_READ_ROOTS", "/workspace,/mnt/ingest").split(",")
    if p.strip()
]

URL_RE = re.compile(r"https?://[^\s)\]>,]+")
REPO_RE = re.compile(r"(?:https?://)?github\.com/[\w.-]+/[\w.-]+(?:\.git)?", re.IGNORECASE)
YOUTUBE_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/)([\w-]{11})",
    re.IGNORECASE,
)


@dataclass
class IngestResult:
    batch_id: str
    obsidian_path: str | None
    artifact_keys: list[str]
    semantic_text_preview: str
    errors: list[str]
    title: str
    tags: list[str]

    def to_dict(self) -> dict:
        return {
            "batch_id": self.batch_id,
            "obsidian_path": self.obsidian_path,
            "artifact_keys": self.artifact_keys,
            "semantic_text_preview": self.semantic_text_preview,
            "errors": self.errors,
            "title": self.title,
            "tags": self.tags,
        }


SYSTEM_PROMPT = """You are the agentibrain KB ingest router. Your job: read an operator message and extract:
  1. `semantic_text`: the semantic/idea/commentary part of the message (the operator's own thinking)
  2. `extractables`: concrete references to CONTENT that should be downloaded/read and stored
  3. `title`: a short descriptive title for this ingest batch (max 80 chars)
  4. `tags`: 2-5 keyword tags for categorization

Return ONLY valid JSON matching this schema:
{
  "semantic_text": "string — operator's own commentary/idea/question",
  "extractables": [
    {"type": "url",        "value": "https://...", "hint": "short description"},
    {"type": "youtube",    "value": "https://youtube.com/watch?v=...", "hint": "video title"},
    {"type": "repo",       "value": "https://github.com/owner/repo", "hint": "..."},
    {"type": "local_path", "value": "/absolute/path/to/file", "hint": "..."}
  ],
  "title": "short title",
  "tags": ["tag1", "tag2"]
}

Rules:
- URLs: include every http(s) URL found in the message
- YouTube: if a URL is youtube.com/watch or youtu.be, tag it as "youtube" (not "url")
- Repos: if a URL is a github.com repo, tag it as "repo" (not "url")
- Local paths: only absolute paths the operator explicitly mentioned as files
- semantic_text must be the operator's OWN words — strip out the URLs/paths
- If there is no semantic content, set semantic_text to the empty string
- If there are no extractables, return an empty list
- Do NOT fabricate references. Only include things the operator actually mentioned.
"""


def _parse_json(raw: str) -> dict:
    if not raw:
        return {}
    if "```" in raw:
        for block in raw.split("```"):
            stripped = block.strip()
            if stripped.startswith("json"):
                stripped = stripped[4:].strip()
            if stripped.startswith("{"):
                try:
                    return json.loads(stripped)
                except json.JSONDecodeError:
                    continue
    stripped = raw.strip()
    if stripped.startswith("{"):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            pass
    return {}


async def _call_router_llm(message: str, model: str) -> dict:
    """Single-shot classification via any OpenAI-compatible gateway.

    Posts a standard chat-completions body with `model: <BRAIN_CLASSIFY_MODEL>`.
    Wire INFERENCE_URL at any OAI-compatible endpoint (LiteLLM, OpenAI, Ollama).
    Fails closed to a deterministic regex classifier if the gateway is down.
    """
    if not INFERENCE_URL:
        log.warning("inference gateway not configured; falling back to regex")
        return _fallback_classify(message)

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": message},
        ],
        "max_tokens": 2048,
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }
    headers = {"Content-Type": "application/json"}
    token = os.environ.get(INFERENCE_TOKEN_ENV, "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{INFERENCE_URL.rstrip('/')}/v1/chat/completions",
                json=body,
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "") or ""
    except Exception as exc:
        log.warning("inference gateway classification failed: %s — using regex fallback", exc)
        return _fallback_classify(message)

    parsed = _parse_json(content)
    if not parsed:
        return _fallback_classify(message)
    parsed.setdefault("semantic_text", "")
    parsed.setdefault("extractables", [])
    parsed.setdefault("title", message[:80].strip() or "ingest")
    parsed.setdefault("tags", [])
    return parsed


def _fallback_classify(message: str) -> dict:
    """Deterministic regex-based classification when the LLM is unavailable or misfires."""
    extractables: list[dict] = []
    remaining = message
    for m in URL_RE.finditer(message):
        val = m.group(0).rstrip(".,;:!?")
        is_repo = "github.com" in val.lower()
        is_youtube = YOUTUBE_RE.search(val) is not None
        if is_youtube:
            etype = "youtube"
        elif is_repo:
            etype = "repo"
        else:
            etype = "url"
        extractables.append({
            "type": etype,
            "value": val,
            "hint": "",
        })
        remaining = remaining.replace(val, " ")
    semantic = " ".join(remaining.split()).strip()
    title = (semantic[:80] or message[:80]).strip() or "ingest"
    return {
        "semantic_text": semantic,
        "extractables": extractables,
        "title": title,
        "tags": [],
    }


def _sanitize_local_path(path: str) -> Path | None:
    """Return the Path iff it is inside one of LOCAL_READ_ROOTS; else None."""
    try:
        candidate = Path(path).resolve()
    except (OSError, ValueError):
        return None
    for root in LOCAL_READ_ROOTS:
        try:
            candidate.relative_to(root)
            if candidate.exists() and candidate.is_file():
                return candidate
        except ValueError:
            continue
    return None


async def _artifact_headers() -> dict:
    return {"Authorization": f"Bearer {ARTIFACT_STORE_KEY}"} if ARTIFACT_STORE_KEY else {}


async def _upload_bytes_to_artifact_store(
    raw: bytes,
    *,
    filename: str,
    content_type: str,
    producer: str,
    artifact_type: str,
    slug: str,
    tags: dict,
    client: httpx.AsyncClient,
) -> dict:
    """Delegate to artifact-store POST /artifacts/upload (multipart)."""
    if not ARTIFACT_STORE_URL:
        raise RuntimeError(
            "ARTIFACT_STORE_URL is not configured; binary ingest is disabled. "
            "Point it at your storage plane (an upstream artifact-store, S3 gateway, "
            "or any OpenAPI-compatible blob service) to enable blob upload."
        )
    files = {"file": (filename, raw, content_type)}
    data = {
        "producer": producer,
        "artifact_type": artifact_type,
        "slug": slug,
        "key_prefix": "raw",
        "tags": json.dumps(tags or {}),
    }
    headers = await _artifact_headers()
    resp = await client.post(
        f"{ARTIFACT_STORE_URL}/artifacts/upload",
        files=files,
        data=data,
        headers=headers,
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()


async def _write_obsidian_note(
    *,
    title: str,
    content: str,
    tags: list[str],
    artifact_refs: list[str],
    client: httpx.AsyncClient,
) -> str | None:
    headers = {"Authorization": f"Bearer {OBSIDIAN_READER_TOKEN}"} if OBSIDIAN_READER_TOKEN else {}
    data = {
        "title": title,
        "content": content,
        "tags": ",".join(tags),
        "artifact_refs": ",".join(artifact_refs),
    }
    try:
        resp = await client.post(
            f"{OBSIDIAN_READER_URL}/write_inbox",
            data=data,
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("path")
    except Exception as exc:
        log.warning("obsidian write failed: %s", exc)
        return None


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (s or "ingest")[:60]


async def _fetch_url_extractable(
    value: str,
    hint: str,
    *,
    batch_id: str,
    client: httpx.AsyncClient,
) -> tuple[str | None, str | None]:
    """Download a URL, PUT to artifact-store, return (artifact_key, error)."""
    try:
        resp = await client.get(value, follow_redirects=True, timeout=60)
        resp.raise_for_status()
        body = resp.content
        ct = resp.headers.get("content-type", "application/octet-stream").split(";")[0].strip()
    except Exception as exc:
        return None, f"fetch failed: {value} — {exc}"

    # Derive filename from the URL
    url_tail = value.rstrip("/").split("/")[-1] or "page"
    url_tail = url_tail.split("?")[0].split("#")[0]
    if "." in url_tail:
        stem, ext = url_tail.rsplit(".", 1)
    else:
        # Infer extension from content-type
        if "html" in ct:
            stem, ext = url_tail, "html"
        elif "pdf" in ct:
            stem, ext = url_tail, "pdf"
        elif "json" in ct:
            stem, ext = url_tail, "json"
        elif "markdown" in ct:
            stem, ext = url_tail, "md"
        elif "text" in ct:
            stem, ext = url_tail, "txt"
        else:
            stem, ext = url_tail, "bin"
    slug = _slugify(hint or stem or "url")
    filename = f"{slug}.{ext}"

    tags = {
        "source": "url",
        "origin_url": value,
        "ingest_batch": batch_id,
    }
    try:
        result = await _upload_bytes_to_artifact_store(
            body,
            filename=filename,
            content_type=ct,
            producer="ingest",
            artifact_type="url",
            slug=slug,
            tags=tags,
            client=client,
        )
        return result.get("key"), None
    except Exception as exc:
        return None, f"upload failed: {value} — {exc}"


async def _clone_and_zip_repo(
    value: str,
    hint: str,
    *,
    batch_id: str,
    client: httpx.AsyncClient,
) -> tuple[str | None, str | None]:
    """Shallow-clone a repo, zip it, PUT to artifact-store."""
    workdir = tempfile.mkdtemp(prefix="kb-router-repo-")
    try:
        repo_name = value.rstrip("/").split("/")[-1]
        if repo_name.endswith(".git"):
            repo_name = repo_name[:-4]
        clone_path = os.path.join(workdir, repo_name)
        try:
            subprocess.run(
                ["git", "clone", "--depth=1", "--quiet", value, clone_path],
                check=True,
                capture_output=True,
                timeout=300,
            )
        except subprocess.CalledProcessError as exc:
            return None, f"git clone failed: {value} — {exc.stderr.decode('utf-8', errors='replace')[:200]}"

        import shutil
        zip_path = os.path.join(workdir, f"{repo_name}.zip")
        shutil.make_archive(zip_path[:-4], "zip", clone_path)
        with open(zip_path, "rb") as f:
            body = f.read()

        slug = _slugify(hint or repo_name)
        filename = f"{slug}.zip"
        tags = {
            "source": "repo",
            "origin_url": value,
            "ingest_batch": batch_id,
            "repo_name": repo_name,
        }
        try:
            result = await _upload_bytes_to_artifact_store(
                body,
                filename=filename,
                content_type="application/zip",
                producer="ingest",
                artifact_type="repo",
                slug=slug,
                tags=tags,
                client=client,
            )
            return result.get("key"), None
        except Exception as exc:
            return None, f"upload failed: {value} — {exc}"
    finally:
        import shutil
        try:
            shutil.rmtree(workdir, ignore_errors=True)
        except OSError:
            pass


async def _read_local_extractable(
    value: str,
    hint: str,
    *,
    batch_id: str,
    client: httpx.AsyncClient,
) -> tuple[str | None, str | None]:
    p = _sanitize_local_path(value)
    if not p:
        return None, f"local path rejected (not in allowlist or missing): {value}"
    try:
        body = p.read_bytes()
    except OSError as exc:
        return None, f"read failed: {value} — {exc}"

    ext = p.suffix.lstrip(".") or "bin"
    # Guess a content-type
    ct_map = {
        "md": "text/markdown", "txt": "text/plain", "pdf": "application/pdf",
        "html": "text/html", "json": "application/json", "csv": "text/csv",
    }
    ct = ct_map.get(ext.lower(), "application/octet-stream")

    slug = _slugify(hint or p.stem)
    filename = p.name
    tags = {
        "source": "local_path",
        "origin_path": str(p),
        "ingest_batch": batch_id,
    }
    try:
        result = await _upload_bytes_to_artifact_store(
            body,
            filename=filename,
            content_type=ct,
            producer="ingest",
            artifact_type="file",
            slug=slug,
            tags=tags,
            client=client,
        )
        return result.get("key"), None
    except Exception as exc:
        return None, f"upload failed: {value} — {exc}"


def _extract_video_id(url: str) -> str | None:
    m = YOUTUBE_RE.search(url)
    return m.group(1) if m else None


async def _fetch_youtube_transcript(
    value: str,
    hint: str,
    *,
    batch_id: str,
    client: httpx.AsyncClient,
) -> tuple[str | None, str | None]:
    """Fetch YouTube transcript via youtube-transcript-api, store as text artifact."""
    video_id = _extract_video_id(value)
    if not video_id:
        return None, f"could not extract video ID from: {value}"

    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
        try:
            transcript = transcript_list.find_transcript(["en"])
        except Exception:
            transcript = transcript_list.find_generated_transcript(["en"])
        entries = transcript.fetch()
        lines = [entry.text for entry in entries]
        text = "\n".join(lines)
    except Exception as exc:
        return None, f"transcript fetch failed: {value} — {exc}"

    if not text.strip():
        return None, f"empty transcript for: {value}"

    slug = _slugify(hint or f"youtube-{video_id}")
    filename = f"{slug}.txt"
    body = f"# YouTube Transcript: {hint or video_id}\n# Source: {value}\n\n{text}".encode("utf-8")
    tags = {
        "source": "youtube",
        "origin_url": value,
        "video_id": video_id,
        "ingest_batch": batch_id,
    }
    try:
        result = await _upload_bytes_to_artifact_store(
            body,
            filename=filename,
            content_type="text/plain",
            producer="ingest",
            artifact_type="transcript",
            slug=slug,
            tags=tags,
            client=client,
        )
        return result.get("key"), None
    except Exception as exc:
        return None, f"upload failed: {value} — {exc}"


async def ingest_message(message: str) -> IngestResult:
    """Main entry point — classify the message and fan out."""
    batch_id = uuid4().hex[:12]
    errors: list[str] = []

    classification = await _call_router_llm(message, BRAIN_CLASSIFY_MODEL)
    semantic = (classification.get("semantic_text") or "").strip()
    title = (classification.get("title") or "").strip() or "ingest"
    tags = classification.get("tags") or []
    extractables = classification.get("extractables") or []

    artifact_keys: list[str] = []

    async with httpx.AsyncClient(timeout=120) as client:
        # Fan out extractables in parallel
        extract_tasks = []
        for ex in extractables:
            etype = (ex.get("type") or "").lower()
            value = ex.get("value") or ""
            hint = ex.get("hint") or ""
            if etype == "youtube":
                extract_tasks.append(_fetch_youtube_transcript(value, hint, batch_id=batch_id, client=client))
            elif etype == "url":
                extract_tasks.append(_fetch_url_extractable(value, hint, batch_id=batch_id, client=client))
            elif etype == "repo":
                extract_tasks.append(_clone_and_zip_repo(value, hint, batch_id=batch_id, client=client))
            elif etype == "local_path":
                extract_tasks.append(_read_local_extractable(value, hint, batch_id=batch_id, client=client))
            else:
                errors.append(f"unknown extractable type: {etype}")

        if extract_tasks:
            results = await asyncio.gather(*extract_tasks, return_exceptions=False)
            for key, err in results:
                if key:
                    artifact_keys.append(key)
                if err:
                    errors.append(err)

        # Write Obsidian note last, so we can include artifact_keys in frontmatter
        obsidian_path: str | None = None
        if semantic or artifact_keys:
            note_content = semantic or f"(no semantic text — ingested {len(artifact_keys)} references)"
            note_content += f"\n\n---\n\n_ingest_batch: {batch_id}_\n_original_message: {message[:500]}_"
            obsidian_path = await _write_obsidian_note(
                title=title,
                content=note_content,
                tags=tags + ["kb-router"],
                artifact_refs=artifact_keys,
                client=client,
            )

    return IngestResult(
        batch_id=batch_id,
        obsidian_path=obsidian_path,
        artifact_keys=artifact_keys,
        semantic_text_preview=semantic[:500],
        errors=errors,
        title=title,
        tags=tags,
    )
