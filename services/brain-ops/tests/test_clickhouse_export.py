from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

_BRAIN_TOOLS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BRAIN_TOOLS))

import brain_tick  # noqa: E402


def test_schema_uses_configured_database_and_table():
    ddl = brain_tick._brain_schema_ddl("telemetry", "brain_ticks")
    assert ddl[0] == "CREATE DATABASE IF NOT EXISTS telemetry"
    assert "telemetry.brain_ticks" in ddl[1]
    assert "telemetry.brain_ticks" in ddl[2]
    assert "vault_bytes UInt64" in ddl[1]
    assert "vault_files UInt32" in ddl[1]


@pytest.mark.parametrize("value", ["brain.tick_health", "brain-name", "1brain", ""])
def test_schema_rejects_unsafe_identifiers(value):
    with pytest.raises(ValueError):
        brain_tick._clickhouse_ident(value)


def test_export_uses_configured_table(monkeypatch):
    requests = []
    monkeypatch.setattr(brain_tick, "CLICKHOUSE_URL", "http://user:pass@clickhouse:8123")
    monkeypatch.setattr(brain_tick, "CLICKHOUSE_DATABASE", "telemetry")
    monkeypatch.setattr(brain_tick, "CLICKHOUSE_TICK_TABLE", "brain_ticks")
    monkeypatch.setattr(brain_tick, "_ch_request", lambda base, sql, auth: requests.append(sql))

    brain_tick._push_clickhouse({"phases": {}, "total_ms": 1})

    assert requests[-1].startswith("INSERT INTO telemetry.brain_ticks ")


def test_export_uses_separate_credentials(monkeypatch):
    requests = []
    monkeypatch.setattr(brain_tick, "CLICKHOUSE_URL", "http://clickhouse:8123")
    monkeypatch.setattr(brain_tick, "CLICKHOUSE_USER", "brain-writer")
    monkeypatch.setattr(brain_tick, "CLICKHOUSE_PASSWORD", "test-value")
    monkeypatch.setattr(
        brain_tick,
        "_ch_request",
        lambda base, sql, auth: requests.append(auth),
    )

    brain_tick._push_clickhouse({"phases": {}, "total_ms": 1})

    expected = base64.b64encode(b"brain-writer:test-value").decode()
    assert requests[-1] == f"Basic {expected}"


def test_export_reuses_last_health_when_ai_is_skipped(monkeypatch, tmp_path):
    requests = []
    (tmp_path / "last-tick-diff.md").write_text("Health score: 8/10\n")
    monkeypatch.setattr(brain_tick, "CLICKHOUSE_URL", "http://clickhouse:8123")
    monkeypatch.setattr(brain_tick, "_ch_request", lambda base, sql, auth: requests.append(sql))

    brain_tick._push_clickhouse({"phases": {}, "total_ms": 1}, tmp_path)

    assert "VALUES (8," in requests[-1]
