"""outbox_drain — replay buffered markers over HTTP with idempotency parity."""

from __future__ import annotations

import json
import sys
import urllib.error
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import outbox_drain  # noqa: E402


def _write_entry(d: Path, name: str, **overrides) -> Path:
    entry = {
        "type": "lesson",
        "content": "buffered lesson content",
        "attrs": {},
        "session_id": "sess-1234",
        "agent_name": "iamroot",
        "project": "/home/x/repo",
        "ts": "2026-05-12T19:27:10+00:00",
    }
    entry.update(overrides)
    p = d / name
    p.write_text(json.dumps(entry))
    return p


def test_marker_request_idempotency_parity_with_agentihooks():
    """Key must equal uuid5('{session_id}-{type}-{content}') — the exact
    formula brain_writer_hook uses, so replays dedupe against originals."""
    entry = {"type": "lesson", "content": "abc", "session_id": "s1", "ts": "2026-01-01T00:00:00Z"}
    body, idem = outbox_drain._marker_request(entry)
    expected = uuid.uuid5(uuid.NAMESPACE_URL, "s1-lesson-abc").hex[:32]
    assert idem == expected
    assert body["attrs"]["ts"] == "2026-01-01T00:00:00Z"
    assert body["attrs"]["session_id"] == "s1"


def test_drain_deletes_on_success(tmp_path, monkeypatch):
    _write_entry(tmp_path, "a.json")
    _write_entry(tmp_path, "b.json")
    posted = []
    monkeypatch.setattr(
        outbox_drain, "_post_marker", lambda url, tok, body, idem: posted.append((body, idem))
    )
    stats = outbox_drain.drain_dir(tmp_path, "http://x", "tok")
    assert stats == {"drained": 2, "quarantined": 0, "failed": 0}
    assert list(tmp_path.glob("*.json")) == []
    assert len(posted) == 2


def test_drain_quarantines_unparseable(tmp_path, monkeypatch):
    (tmp_path / "bad.json").write_text("{not json")
    monkeypatch.setattr(outbox_drain, "_post_marker", lambda *a: None)
    stats = outbox_drain.drain_dir(tmp_path, "http://x", "tok")
    assert stats["quarantined"] == 1
    assert (tmp_path / "bad.bad").exists()


def test_drain_quarantines_on_4xx_leaves_on_5xx(tmp_path, monkeypatch):
    _write_entry(tmp_path, "rejected.json", content="x" * 10)
    _write_entry(tmp_path, "transient.json", content="y" * 10)

    def fake_post(url, tok, body, idem):
        code = 400 if body["content"].startswith("x") else 503
        raise urllib.error.HTTPError(url, code, "boom", None, None)

    monkeypatch.setattr(outbox_drain, "_post_marker", fake_post)
    stats = outbox_drain.drain_dir(tmp_path, "http://x", "tok")
    assert stats == {"drained": 0, "quarantined": 1, "failed": 1}
    assert (tmp_path / "rejected.bad").exists()
    assert (tmp_path / "transient.json").exists()


def test_drain_missing_dir_is_noop(tmp_path):
    stats = outbox_drain.drain_dir(tmp_path / "absent", "http://x", "tok")
    assert stats == {"drained": 0, "quarantined": 0, "failed": 0}
