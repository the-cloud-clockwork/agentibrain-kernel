"""Ingest router — one LLM call (Haiku) to classify, then write everything to the vault.

Flow:
  1. LLM extracts: semantic_text, extractables (urls/repos/local_paths), title, tags
  2. Extractables → fetch/clone/read → write to vault as raw/inbox/ text files
  3. Semantic text + references → write vault note (via vault_reader.write_inbox)
  4. Return what was created

No external dependencies. Everything lands in the vault filesystem.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx

from . import vault_reader

log = logging.getLogger("brain_api")

INFERENCE_URL = os.getenv("INFERENCE_URL", "")
INFERENCE_TOKEN_ENV = "INFERENCE_API_KEY"
BRAIN_CLASSIFY_MODEL = os.getenv("BRAIN_CLASSIFY_MODEL", "brain-classify")

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
    vault_paths: list[str] = field(default_factory=list)
    semantic_text_preview: str = ""
    errors: list[str] = field(default_factory=list)
    title: str = ""
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "batch_id": self.batch_id,
            "obsidian_path": self.obsidian_path,
            "vault_paths": self.vault_paths,
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
        extractables.append({"type": etype, "value": val, "hint": ""})
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


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (s or "ingest")[:60]


def _write_extractable_to_vault(title: str, content: str, tags: list[str], batch_id: str) -> str | None:
    """Write an extracted piece of content to the vault inbox."""
    try:
        result = vault_reader.write_inbox(
            title=title,
            content=content,
            tags=tags,
            artifact_refs=[],
        )
        return result.get("path")
    except Exception as exc:
        log.warning("vault write failed for extractable %s: %s", title, exc)
        return None


async def _fetch_url(value: str, hint: str, batch_id: str) -> tuple[str | None, str | None]:
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            resp = await client.get(value)
            resp.raise_for_status()
            text = resp.text
    except Exception as exc:
        return None, f"fetch failed: {value} — {exc}"

    if not text.strip():
        return None, f"empty content from: {value}"

    slug = _slugify(hint or value.rstrip("/").split("/")[-1].split("?")[0])
    title = f"URL: {hint or slug} ({value})"
    body = f"# {title}\n\nSource: {value}\nFetched: {datetime.now(timezone.utc).isoformat()}\n\n---\n\n{text[:500_000]}"
    path = await asyncio.to_thread(
        _write_extractable_to_vault, title=slug, content=body, tags=["url", "extracted"], batch_id=batch_id,
    )
    return path, None if path else f"vault write failed for {value}"


async def _fetch_youtube_transcript(value: str, hint: str, batch_id: str) -> tuple[str | None, str | None]:
    video_id = YOUTUBE_RE.search(value)
    if not video_id:
        return None, f"could not extract video ID from: {value}"
    video_id = video_id.group(1)

    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
        try:
            transcript = transcript_list.find_transcript(["en"])
        except Exception:
            transcript = transcript_list.find_generated_transcript(["en"])
        entries = transcript.fetch()
        text = "\n".join(entry.text for entry in entries)
    except Exception as exc:
        return None, f"transcript fetch failed: {value} — {exc}"

    if not text.strip():
        return None, f"empty transcript for: {value}"

    slug = _slugify(hint or f"youtube-{video_id}")
    body = f"# YouTube Transcript: {hint or video_id}\n\nSource: {value}\nVideo ID: {video_id}\n\n---\n\n{text}"
    path = await asyncio.to_thread(
        _write_extractable_to_vault, title=slug, content=body, tags=["youtube", "transcript", "extracted"], batch_id=batch_id,
    )
    return path, None if path else f"vault write failed for {value}"


async def _clone_and_read_repo(value: str, hint: str, batch_id: str) -> tuple[str | None, str | None]:
    workdir = tempfile.mkdtemp(prefix="brain-api-repo-")
    try:
        repo_name = value.rstrip("/").split("/")[-1]
        if repo_name.endswith(".git"):
            repo_name = repo_name[:-4]
        clone_path = os.path.join(workdir, repo_name)
        try:
            subprocess.run(
                ["git", "clone", "--depth=1", "--quiet", value, clone_path],
                check=True, capture_output=True, timeout=300,
            )
        except subprocess.CalledProcessError as exc:
            return None, f"git clone failed: {value} — {exc.stderr.decode('utf-8', errors='replace')[:200]}"

        readme_path = Path(clone_path) / "README.md"
        if not readme_path.exists():
            for candidate in Path(clone_path).glob("README*"):
                readme_path = candidate
                break

        parts = [f"# Repository: {repo_name}\n\nSource: {value}\nCloned: {datetime.now(timezone.utc).isoformat()}\n"]
        if readme_path.exists():
            parts.append(f"## README\n\n{readme_path.read_text(errors='replace')[:100_000]}")

        tree_lines = []
        for p in sorted(Path(clone_path).rglob("*")):
            if ".git" in p.parts:
                continue
            rel = p.relative_to(clone_path)
            tree_lines.append(str(rel))
        if tree_lines:
            parts.append(f"## File Tree\n\n```\n{chr(10).join(tree_lines[:500])}\n```")

        body = "\n\n---\n\n".join(parts)
        slug = _slugify(hint or repo_name)
        path = await asyncio.to_thread(
            _write_extractable_to_vault, title=slug, content=body[:500_000], tags=["repo", "extracted"], batch_id=batch_id,
        )
        return path, None if path else f"vault write failed for {value}"
    finally:
        import shutil
        shutil.rmtree(workdir, ignore_errors=True)


async def _read_local_file(value: str, hint: str, batch_id: str) -> tuple[str | None, str | None]:
    p = _sanitize_local_path(value)
    if not p:
        return None, f"local path rejected (not in allowlist or missing): {value}"
    try:
        text = p.read_text(errors="replace")
    except OSError as exc:
        return None, f"read failed: {value} — {exc}"

    slug = _slugify(hint or p.stem)
    body = f"# Local File: {p.name}\n\nPath: {value}\nRead: {datetime.now(timezone.utc).isoformat()}\n\n---\n\n{text[:500_000]}"
    path = await asyncio.to_thread(
        _write_extractable_to_vault, title=slug, content=body, tags=["local-file", "extracted"], batch_id=batch_id,
    )
    return path, None if path else f"vault write failed for {value}"


async def _write_vault_note(
    *,
    title: str,
    content: str,
    tags: list[str],
    vault_refs: list[str],
) -> str | None:
    extra = ""
    if vault_refs:
        links = "\n".join(f"- [[{Path(r).stem}]]" for r in vault_refs)
        extra = f"\n\n## Related Vault Notes\n\n{links}"
    try:
        result = await asyncio.to_thread(
            vault_reader.write_inbox,
            title=title, content=content + extra, tags=tags, artifact_refs=[],
        )
        return result.get("path")
    except Exception as exc:
        log.warning("vault write failed: %s", exc)
        return None


async def ingest_message(message: str) -> IngestResult:
    """Main entry point — classify the message and fan out to vault."""
    batch_id = uuid4().hex[:12]
    errors: list[str] = []

    classification = await _call_router_llm(message, BRAIN_CLASSIFY_MODEL)
    semantic = (classification.get("semantic_text") or "").strip()
    title = (classification.get("title") or "").strip() or "ingest"
    tags = classification.get("tags") or []
    extractables = classification.get("extractables") or []

    vault_paths: list[str] = []

    extract_tasks = []
    for ex in extractables:
        etype = (ex.get("type") or "").lower()
        value = ex.get("value") or ""
        hint = ex.get("hint") or ""
        if etype == "youtube":
            extract_tasks.append(_fetch_youtube_transcript(value, hint, batch_id))
        elif etype == "url":
            extract_tasks.append(_fetch_url(value, hint, batch_id))
        elif etype == "repo":
            extract_tasks.append(_clone_and_read_repo(value, hint, batch_id))
        elif etype == "local_path":
            extract_tasks.append(_read_local_file(value, hint, batch_id))
        else:
            errors.append(f"unknown extractable type: {etype}")

    if extract_tasks:
        results = await asyncio.gather(*extract_tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                errors.append(str(r))
            else:
                path, err = r
                if path:
                    vault_paths.append(path)
                if err:
                    errors.append(err)

    obsidian_path: str | None = None
    if semantic or vault_paths:
        note_content = semantic or f"(no semantic text — ingested {len(vault_paths)} references)"
        note_content += f"\n\n---\n\n_ingest_batch: {batch_id}_\n_original_message: {message[:500]}_"
        obsidian_path = await _write_vault_note(
            title=title,
            content=note_content,
            tags=tags + ["brain-api"],
            vault_refs=vault_paths,
        )

    return IngestResult(
        batch_id=batch_id,
        obsidian_path=obsidian_path,
        vault_paths=vault_paths,
        semantic_text_preview=semantic[:500],
        errors=errors,
        title=title,
        tags=tags,
    )
