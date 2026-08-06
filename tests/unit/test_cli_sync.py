"""agentibrain sync — buffered-marker replay with idempotency + ts fidelity."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import httpx

from agentibrain import cli


class _Resp:
    def __init__(self, status_code=201):
        self.status_code = status_code


def _entry(d: Path, name: str, **overrides) -> Path:
    entry = {
        "type": "milestone",
        "content": "shipped the thing",
        "attrs": {},
        "session_id": "sess-1",
        "agent_name": "iamroot",
        "project": "/home/x/repo",
        "ts": "2026-05-12T19:27:10+00:00",
    }
    entry.update(overrides)
    p = d / name
    p.write_text(json.dumps(entry))
    return p


def test_drain_posts_with_parity_key_and_ts(tmp_path, monkeypatch):
    _entry(tmp_path, "a.json")
    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen["url"] = url
        seen["idem"] = headers.get("X-Idempotency-Key")
        seen["body"] = json
        return _Resp(201)

    monkeypatch.setattr(cli.httpx, "post", fake_post)
    stats = cli._drain_marker_dir(tmp_path, "http://b", {"Authorization": "Bearer t"})
    assert stats == {"drained": 1, "quarantined": 0, "failed": 0}
    assert seen["url"] == "http://b/marker"
    assert (
        seen["idem"]
        == uuid.uuid5(uuid.NAMESPACE_URL, "sess-1-milestone-shipped the thing").hex[:32]
    )
    assert seen["body"]["attrs"]["ts"] == "2026-05-12T19:27:10+00:00"
    assert seen["body"]["attrs"]["source"] == "iamroot"
    assert list(tmp_path.glob("*.json")) == []


def test_drain_leaves_files_on_transport_failure(tmp_path, monkeypatch):
    _entry(tmp_path, "a.json")

    def fake_post(url, **kw):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(cli.httpx, "post", fake_post)
    stats = cli._drain_marker_dir(tmp_path, "http://b", {})
    assert stats == {"drained": 0, "quarantined": 0, "failed": 1}
    assert (tmp_path / "a.json").exists()


def test_drain_quarantines_rejects_and_garbage(tmp_path, monkeypatch):
    (tmp_path / "garbage.json").write_text("{nope")
    _entry(tmp_path, "rejected.json")
    monkeypatch.setattr(cli.httpx, "post", lambda url, **kw: _Resp(400))
    stats = cli._drain_marker_dir(tmp_path, "http://b", {})
    assert stats == {"drained": 0, "quarantined": 2, "failed": 0}
    assert (tmp_path / "garbage.bad").exists()
    assert (tmp_path / "rejected.bad").exists()


def test_drain_missing_dir_noop(tmp_path):
    stats = cli._drain_marker_dir(tmp_path / "absent", "http://b", {})
    assert stats == {"drained": 0, "quarantined": 0, "failed": 0}


def test_drain_retries_on_credential_rejection(tmp_path, monkeypatch):
    """A stale token (401) must leave the queue intact, not quarantine it."""
    _entry(tmp_path, "a.json")
    monkeypatch.setattr(cli.httpx, "post", lambda url, **kw: _Resp(401))
    stats = cli._drain_marker_dir(tmp_path, "http://b", {})
    assert stats == {"drained": 0, "quarantined": 0, "failed": 1}
    assert (tmp_path / "a.json").exists()


def test_drain_verbose_announces_buffer_size(tmp_path, monkeypatch, capsys):
    _entry(tmp_path, "a.json")
    monkeypatch.setattr(cli.httpx, "post", lambda url, **kw: _Resp(201))
    cli._drain_marker_dir(tmp_path, "http://b", {}, verbose=True)
    assert "1 buffered file(s) to replay" in capsys.readouterr().out


def test_sync_help_offers_check_flag():
    from click.testing import CliRunner

    result = CliRunner().invoke(cli.main, ["sync", "--help"])
    assert result.exit_code == 0
    assert "--check" in result.output
    assert "--wait" in result.output


def test_sync_reports_empty_buffers(tmp_path, monkeypatch, capsys):
    """Empty buffers must say so, not print meaningless zero counters."""
    import click.testing

    outbox = tmp_path / "brain-outbox"
    outbox.mkdir()
    monkeypatch.setenv("BRAIN_WRITER_OUTBOX", str(outbox))
    monkeypatch.setenv("KB_ROUTER_TOKEN", "t")

    class _TickResp:
        status_code = 202

        def raise_for_status(self):
            pass

        def json(self):
            return {"job_id": "j1"}

    monkeypatch.setattr(cli.httpx, "post", lambda url, **kw: _TickResp())
    result = click.testing.CliRunner().invoke(cli.main, ["sync"])
    assert result.exit_code == 0, result.output
    assert "empty — nothing to replay" in result.output
    assert "drained=0" not in result.output
