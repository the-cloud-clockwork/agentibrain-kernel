"""The embedder must never prune on an empty scan.

`/prune` deletes every row for a producer whose key is absent from `keep_keys`,
so an empty keep set erases the whole index for that producer. A scan can come
back empty for reasons that have nothing to do with the operator deleting
anything — an unmounted vault, an NFS blip — and the loss is permanent rather
than self-correcting: the mtime state file still records every file as
embedded, so the next run skips them all and nothing rebuilds short of
`--force-all`.

Run from repo root:
    pytest -q services/brain-ops/tests
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_BRAIN_TOOLS = _HERE.parent
if str(_BRAIN_TOOLS) not in sys.path:
    sys.path.insert(0, str(_BRAIN_TOOLS))

import embed_arcs  # noqa: E402


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def _run_with_captured_posts(monkeypatch, vault: Path, argv: list[str]) -> list[dict]:
    """Run embed_arcs.main(), capturing every POST instead of sending it."""
    posts: list[dict] = []

    def fake_urlopen(req, *_a, **_k):
        body = json.loads(req.data.decode("utf-8")) if req.data else {}
        posts.append({"url": req.full_url, "body": body})
        return _FakeResponse({"deleted": 0, "kept": 0, "chunks_stored": 1})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(sys, "argv", ["embed_arcs.py", "--vault", str(vault), *argv])
    embed_arcs.main()
    return posts


def _prune_bodies(posts: list[dict], producer: str) -> list[dict]:
    return [
        p["body"]
        for p in posts
        if p["url"].endswith("/prune") and p["body"].get("producer") == producer
    ]


def test_no_prune_when_no_arcs_scanned(tmp_path: Path, monkeypatch):
    """An empty vault must not erase the arc index."""
    vault = tmp_path / "vault"
    (vault / "left" / "reference").mkdir(parents=True)

    posts = _run_with_captured_posts(
        monkeypatch, vault, ["--prune", "--api-key", "k"]
    )

    assert _prune_bodies(posts, "brain-arc") == []


def test_no_prune_when_no_lesson_logs_scanned(tmp_path: Path, monkeypatch):
    """left/reference/ present but empty must not erase the lesson index."""
    vault = tmp_path / "vault"
    (vault / "left" / "reference").mkdir(parents=True)

    posts = _run_with_captured_posts(
        monkeypatch, vault, ["--prune", "--api-key", "k"]
    )

    assert _prune_bodies(posts, "brain-lesson") == []


def test_prune_runs_with_a_populated_keep_set(tmp_path: Path, monkeypatch):
    """The guard must not disable legitimate reaping."""
    vault = tmp_path / "vault"
    ref = vault / "left" / "reference"
    ref.mkdir(parents=True)
    (ref / "lessons-2026-08-10.md").write_text(
        "---\nid: lessons-2026-08-10\ntitle: Lessons — 2026-08-10\n---\n\n"
        "## 2026-08-10T10:00:00+00:00 — agent\n\n"
        "A lesson long enough to clear the minimum content length check.\n"
    )

    posts = _run_with_captured_posts(
        monkeypatch, vault, ["--prune", "--api-key", "k"]
    )

    bodies = _prune_bodies(posts, "brain-lesson")
    assert len(bodies) == 1
    assert bodies[0]["keep_keys"] == ["lessons-2026-08-10"]
