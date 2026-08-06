"""annotate_fail — failed tick requests carry the tick's log tail."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import annotate_fail  # noqa: E402


def _run(monkeypatch, req: Path, log: Path) -> None:
    monkeypatch.setattr(sys, "argv", ["annotate_fail", str(req), str(log)])
    assert annotate_fail.main() == 0


def test_stamps_error_tail(tmp_path, monkeypatch):
    req = tmp_path / "job.json"
    req.write_text(json.dumps({"job_id": "j1", "status": "pending"}))
    log = tmp_path / "tick.log"
    log.write_text("phase 1 ok\nphase 2 ok\nTraceback: brain_tick exploded\n")
    _run(monkeypatch, req, log)
    data = json.loads(req.read_text())
    assert "brain_tick exploded" in data["error_tail"]
    assert data["job_id"] == "j1"


def test_tail_bounded_to_last_2kb(tmp_path, monkeypatch):
    req = tmp_path / "job.json"
    req.write_text(json.dumps({"job_id": "j1"}))
    log = tmp_path / "tick.log"
    log.write_text("x" * 10_000 + "THE-END")
    _run(monkeypatch, req, log)
    data = json.loads(req.read_text())
    assert len(data["error_tail"]) == annotate_fail.TAIL_CHARS
    assert data["error_tail"].endswith("THE-END")


def test_never_raises_on_garbage_inputs(tmp_path, monkeypatch):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    log = tmp_path / "tick.log"
    log.write_text("some error")
    _run(monkeypatch, bad, log)  # unparseable request → untouched, exit 0
    assert bad.read_text() == "{not json"
    req = tmp_path / "job.json"
    req.write_text(json.dumps({"job_id": "j1"}))
    _run(monkeypatch, req, tmp_path / "missing.log")  # absent log → untouched
    assert "error_tail" not in json.loads(req.read_text())
