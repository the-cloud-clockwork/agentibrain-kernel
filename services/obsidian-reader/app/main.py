"""Obsidian vault reader — read-only filesystem access to the Obsidian vault.

Exposes a minimal FastAPI surface for federated KB search:
  GET /health             → liveness
  GET /list?prefix=...    → list vault files (relative paths)
  GET /read?path=...      → read a single file's content
  GET /search?q=...       → substring search across .md files with snippets
  POST /write             → write to raw/inbox/ ONLY (guarded, used by kb-router)

Authentication: Bearer token via VAULT_READER_TOKENS (comma-separated).
All paths are validated to stay within the mounted vault root (no traversal).
Writes are restricted to the RAW_INBOX_PREFIX subdirectory.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Query


VAULT_ROOT = Path(os.environ.get("VAULT_ROOT", "/vault")).resolve()
RAW_INBOX_PREFIX = os.environ.get("RAW_INBOX_PREFIX", "raw/inbox").strip("/")
MAX_FILE_BYTES = int(os.environ.get("MAX_FILE_BYTES", str(5 * 1024 * 1024)))  # 5 MB
SEARCH_EXTENSIONS = {".md", ".markdown", ".txt"}
SEARCH_SKIP_DIRS = {".git", ".obsidian", ".trash", "node_modules"}

_TOKENS = [t.strip() for t in os.environ.get("VAULT_READER_TOKENS", "").split(",") if t.strip()]


def require_token(authorization: str | None = Header(None)) -> None:
    if not _TOKENS:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    if token not in _TOKENS:
        raise HTTPException(status_code=401, detail="invalid token")


def _resolve_inside_vault(rel_path: str) -> Path:
    """Resolve a relative vault path; raise 400 on traversal."""
    if not rel_path:
        raise HTTPException(400, "path required")
    p = (VAULT_ROOT / rel_path.lstrip("/")).resolve()
    try:
        p.relative_to(VAULT_ROOT)
    except ValueError:
        raise HTTPException(400, "path escapes vault root")
    return p


app = FastAPI(title="Obsidian Vault Reader", version="0.1.0")


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "service": "obsidian-reader",
        "vault_root": str(VAULT_ROOT),
        "vault_mounted": VAULT_ROOT.exists(),
    }


@app.get("/list")
def list_files(
    prefix: str = Query("", description="Directory prefix to list (relative to vault root)"),
    extensions: str = Query(".md,.markdown,.txt", description="Comma-separated extensions to include"),
    limit: int = Query(500, ge=1, le=5000),
    _: None = Depends(require_token),
) -> dict:
    base = _resolve_inside_vault(prefix) if prefix else VAULT_ROOT
    if not base.exists():
        return {"prefix": prefix, "count": 0, "files": []}
    if base.is_file():
        return {
            "prefix": prefix,
            "count": 1,
            "files": [str(base.relative_to(VAULT_ROOT))],
        }

    ext_set = {e.strip() if e.startswith(".") else f".{e.strip()}" for e in extensions.split(",") if e.strip()}

    hits: list[str] = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in SEARCH_SKIP_DIRS]
        for fn in filenames:
            if ext_set and Path(fn).suffix.lower() not in ext_set:
                continue
            rel = Path(dirpath, fn).relative_to(VAULT_ROOT)
            hits.append(str(rel))
            if len(hits) >= limit:
                return {"prefix": prefix, "count": len(hits), "files": sorted(hits), "truncated": True}
    hits.sort()
    return {"prefix": prefix, "count": len(hits), "files": hits}


@app.get("/read")
def read_file(
    path: str = Query(..., description="Relative path inside vault"),
    max_bytes: int = Query(MAX_FILE_BYTES, ge=1, le=MAX_FILE_BYTES),
    _: None = Depends(require_token),
) -> dict:
    p = _resolve_inside_vault(path)
    if not p.exists():
        raise HTTPException(404, "not found")
    if not p.is_file():
        raise HTTPException(400, "path is not a file")
    size = p.stat().st_size
    with open(p, "rb") as f:
        raw = f.read(max_bytes)
    truncated = size > len(raw)
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        content = raw.decode("utf-8", errors="replace")
    return {
        "path": str(p.relative_to(VAULT_ROOT)),
        "size_bytes": size,
        "content": content,
        "truncated": truncated,
    }


@app.get("/search")
def search_vault(
    q: str = Query(..., min_length=1, description="Search query (substring, case-insensitive)"),
    prefix: str = Query("", description="Limit to a subdirectory"),
    limit: int = Query(20, ge=1, le=200),
    context_lines: int = Query(2, ge=0, le=10),
    _: None = Depends(require_token),
) -> dict:
    """Tokenized search over vault text files.

    Multi-word queries are split into tokens and matched with OR logic.
    Score = sum of per-token hit counts + bonus for filename match + bonus
    for files matching ALL tokens. Single-word queries behave as before.
    """
    base = _resolve_inside_vault(prefix) if prefix else VAULT_ROOT
    if not base.exists():
        return {"query": q, "count": 0, "results": []}

    tokens = q.lower().split()
    if not tokens:
        return {"query": q, "count": 0, "results": []}
    patterns = [re.compile(re.escape(t), re.IGNORECASE) for t in tokens]
    results: list[dict] = []

    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in SEARCH_SKIP_DIRS]
        for fn in filenames:
            if Path(fn).suffix.lower() not in SEARCH_EXTENSIONS:
                continue
            full = Path(dirpath, fn)
            rel = str(full.relative_to(VAULT_ROOT))
            fn_lower = fn.lower()
            filename_token_hits = sum(1 for t in tokens if t in fn_lower)
            try:
                with open(full, encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
            except OSError:
                continue

            hit_indices: list[int] = []
            tokens_found: set[int] = set()
            for i, line in enumerate(lines):
                for ti, pat in enumerate(patterns):
                    if pat.search(line):
                        if i not in hit_indices:
                            hit_indices.append(i)
                        tokens_found.add(ti)

            if not hit_indices and not filename_token_hits:
                continue

            snippets: list[dict] = []
            for idx in hit_indices[:3]:
                start = max(0, idx - context_lines)
                end = min(len(lines), idx + context_lines + 1)
                snippet = "".join(lines[start:end]).rstrip()
                snippets.append({"line": idx + 1, "snippet": snippet})

            all_tokens_matched = len(tokens_found) == len(tokens)
            score = (
                len(hit_indices)
                + (10 * filename_token_hits)
                + (20 if all_tokens_matched else 0)
            )
            results.append({
                "path": rel,
                "score": score,
                "match_count": len(hit_indices),
                "tokens_matched": len(tokens_found),
                "tokens_total": len(tokens),
                "filename_match": filename_token_hits > 0,
                "snippets": snippets,
                "title": Path(fn).stem,
            })

    results.sort(key=lambda r: (-r["score"], r["path"]))
    results = results[:limit]
    return {"query": q, "count": len(results), "results": results}


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text: str, max_len: int = 60) -> str:
    s = _SLUG_RE.sub("-", (text or "").lower()).strip("-")
    return (s or "note")[:max_len]


@app.post("/write_inbox")
def write_inbox(
    title: str = Form(..., max_length=200),
    content: str = Form(..., max_length=200_000),
    tags: str = Form("", description="Comma-separated tags for YAML frontmatter"),
    artifact_refs: str = Form("", description="Comma-separated artifact keys to cross-reference"),
    _: None = Depends(require_token),
) -> dict:
    """Write a new note to raw/inbox/. This is the ONLY allowed write path.

    Returns the relative vault path of the created note.
    """
    inbox_root = _resolve_inside_vault(RAW_INBOX_PREFIX)
    inbox_root.mkdir(parents=True, exist_ok=True)

    date_part = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    slug = _slugify(title)
    rel_path = f"{RAW_INBOX_PREFIX}/{date_part}-{slug}.md"
    target = _resolve_inside_vault(rel_path)

    # Avoid overwriting — append short id if collision
    if target.exists():
        from uuid import uuid4
        rel_path = f"{RAW_INBOX_PREFIX}/{date_part}-{slug}-{uuid4().hex[:6]}.md"
        target = _resolve_inside_vault(rel_path)

    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    ref_list = [r.strip() for r in artifact_refs.split(",") if r.strip()]
    frontmatter_lines = ["---", f"title: {title}", f"created: {datetime.now(timezone.utc).isoformat()}"]
    if tag_list:
        frontmatter_lines.append("tags:")
        for t in tag_list:
            frontmatter_lines.append(f"  - {t}")
    if ref_list:
        frontmatter_lines.append("artifact_refs:")
        for r in ref_list:
            frontmatter_lines.append(f"  - {r}")
    frontmatter_lines.append("---")
    frontmatter_lines.append("")

    target.write_text("\n".join(frontmatter_lines) + content + ("" if content.endswith("\n") else "\n"), encoding="utf-8")
    return {
        "path": rel_path,
        "size_bytes": target.stat().st_size,
        "artifact_refs": ref_list,
        "tags": tag_list,
    }
