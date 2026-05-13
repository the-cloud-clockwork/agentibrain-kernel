"""Vault reader — direct filesystem access to the Obsidian vault.

Absorbed from the former obsidian-reader microservice. All operations are
local filesystem calls against the NFS-mounted vault at VAULT_ROOT.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path

VAULT_ROOT = Path(os.environ.get("VAULT_ROOT", "/vault")).resolve()
RAW_INBOX_PREFIX = os.environ.get("RAW_INBOX_PREFIX", "raw/inbox").strip("/")
MAX_FILE_BYTES = int(os.environ.get("MAX_FILE_BYTES", str(5 * 1024 * 1024)))
SEARCH_EXTENSIONS = {".md", ".markdown", ".txt"}
SEARCH_SKIP_DIRS = {".git", ".obsidian", ".trash", "node_modules"}

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def resolve_inside_vault(rel_path: str) -> Path:
    """Resolve a relative vault path; raise ValueError on traversal."""
    if not rel_path:
        raise ValueError("path required")
    p = (VAULT_ROOT / rel_path.lstrip("/")).resolve()
    try:
        p.relative_to(VAULT_ROOT)
    except ValueError:
        raise ValueError("path escapes vault root")
    return p


def list_files(
    prefix: str = "",
    extensions: str = ".md,.markdown,.txt",
    limit: int = 500,
) -> dict:
    base = resolve_inside_vault(prefix) if prefix else VAULT_ROOT
    if not base.exists():
        return {"prefix": prefix, "count": 0, "files": []}
    if base.is_file():
        return {
            "prefix": prefix,
            "count": 1,
            "files": [str(base.relative_to(VAULT_ROOT))],
        }

    ext_set = {
        e.strip() if e.startswith(".") else f".{e.strip()}"
        for e in extensions.split(",")
        if e.strip()
    }

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


def read_file(path: str, max_bytes: int | None = None) -> dict:
    if max_bytes is None:
        max_bytes = MAX_FILE_BYTES
    p = resolve_inside_vault(path)
    if not p.exists():
        raise FileNotFoundError(f"not found: {path}")
    if not p.is_file():
        raise ValueError("path is not a file")
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


def search_vault(
    q: str,
    prefix: str = "",
    limit: int = 20,
    context_lines: int = 2,
) -> dict:
    base = resolve_inside_vault(prefix) if prefix else VAULT_ROOT
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


def _slugify(text: str, max_len: int = 60) -> str:
    s = _SLUG_RE.sub("-", (text or "").lower()).strip("-")
    return (s or "note")[:max_len]


def write_inbox(
    title: str,
    content: str,
    tags: list[str] | None = None,
    artifact_refs: list[str] | None = None,
) -> dict:
    """Write a new note to raw/inbox/. Returns the relative vault path."""
    tags = tags or []
    artifact_refs = artifact_refs or []

    inbox_root = resolve_inside_vault(RAW_INBOX_PREFIX)
    inbox_root.mkdir(parents=True, exist_ok=True)

    date_part = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    slug = _slugify(title)
    rel_path = f"{RAW_INBOX_PREFIX}/{date_part}-{slug}.md"
    target = resolve_inside_vault(rel_path)

    try:
        target.open("x").close()
    except FileExistsError:
        from uuid import uuid4
        rel_path = f"{RAW_INBOX_PREFIX}/{date_part}-{slug}-{uuid4().hex[:6]}.md"
        target = resolve_inside_vault(rel_path)

    frontmatter_lines = [
        "---",
        f"title: {title}",
        f"created: {datetime.now(timezone.utc).isoformat()}",
    ]
    if tags:
        frontmatter_lines.append("tags:")
        for t in tags:
            frontmatter_lines.append(f"  - {t}")
    if artifact_refs:
        frontmatter_lines.append("artifact_refs:")
        for r in artifact_refs:
            frontmatter_lines.append(f"  - {r}")
    frontmatter_lines.append("---")
    frontmatter_lines.append("")

    target.write_text(
        "\n".join(frontmatter_lines) + content + ("" if content.endswith("\n") else "\n"),
        encoding="utf-8",
    )
    return {
        "path": rel_path,
        "size_bytes": target.stat().st_size,
        "artifact_refs": artifact_refs,
        "tags": tags,
    }
