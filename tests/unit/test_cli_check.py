"""agentibrain check — the two probes, the exit contract, and safe rendering.

The exit code is the part other things depend on (CI steps, scripts, the
operator's shell), so it is pinned here rather than left to manual reading:
0 clean · 1 broken or unreachable · 2 degraded.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from agentibrain import cli


class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload) if isinstance(payload, (dict, list)) else str(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


_DEPS_OK = {"status": "ok", "checks": {"vault": {"ok": True, "root": "/vault"}}}


def _pipeline(status="ok", stages=None):
    return {
        "status": status,
        "service": "brain-api",
        "stages": stages or {"feed": {"status": "ok", "served_entries": 3}},
    }


@pytest.fixture()
def isolated(tmp_path: Path, monkeypatch):
    """No local deployment, no local outbox — the remote-brain case."""
    monkeypatch.setenv("KB_ROUTER_TOKEN", "t0ken")
    monkeypatch.setenv("BRAIN_WRITER_OUTBOX", str(tmp_path / "nonexistent-outbox"))
    monkeypatch.setattr(cli, "_load_settings", lambda: cli.BrainSettings(_env_file=None))
    return tmp_path


def _route(monkeypatch, mapping):
    """Serve each endpoint path from `mapping`; anything else raises."""

    def fake_get(url, headers=None, timeout=None):
        for suffix, resp in mapping.items():
            if url.endswith(suffix):
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise AssertionError(f"unexpected GET {url}")

    monkeypatch.setattr(httpx, "get", fake_get)


def test_clean_run_exits_zero(isolated, monkeypatch):
    _route(
        monkeypatch,
        {"/health/deep": _Resp(_DEPS_OK), "/health/pipeline": _Resp(_pipeline("ok"))},
    )
    result = CliRunner().invoke(cli.main, ["check"])
    assert result.exit_code == 0, result.output
    assert "all checks passed" in result.output


def test_a_degraded_pipeline_exits_two_not_one(isolated, monkeypatch):
    """Degraded means flowing-but-behind. Scripts distinguish it from broken."""
    _route(
        monkeypatch,
        {"/health/deep": _Resp(_DEPS_OK), "/health/pipeline": _Resp(_pipeline("degraded"))},
    )
    result = CliRunner().invoke(cli.main, ["check"])
    assert result.exit_code == 2, result.output


def test_a_broken_pipeline_exits_one(isolated, monkeypatch):
    _route(
        monkeypatch,
        {"/health/deep": _Resp(_DEPS_OK), "/health/pipeline": _Resp(_pipeline("broken"))},
    )
    assert CliRunner().invoke(cli.main, ["check"]).exit_code == 1


def test_a_failing_dependency_stays_a_hard_failure(isolated, monkeypatch):
    """/health/deep only ever says "degraded"; the pre-existing contract for
    that was exit 1, and nothing scripting `check` should silently start
    treating an unreachable Postgres as a soft warning."""
    _route(
        monkeypatch,
        {
            "/health/deep": _Resp({"status": "degraded", "checks": {"embeddings": {"ok": False}}}),
            "/health/pipeline": _Resp(_pipeline("ok")),
        },
    )
    assert CliRunner().invoke(cli.main, ["check"]).exit_code == 1


def test_an_unreachable_brain_exits_one_without_the_404_advice(isolated, monkeypatch):
    """A connection timeout is not an outdated image — saying so sends the
    operator to rebuild a container when the real problem is the network."""
    _route(monkeypatch, {"/health/pipeline": httpx.ConnectTimeout("timed out")})
    result = CliRunner().invoke(cli.main, ["check", "--pipeline-only"])
    assert result.exit_code == 1
    assert "ConnectTimeout" in result.output
    assert "predates" not in result.output


def test_an_old_brain_api_without_the_endpoint_says_so(isolated, monkeypatch):
    """FastAPI answers an unknown route with a *valid* JSON body.

    Parsing the body before checking the status accepted `{"detail":"Not
    Found"}` as a health report: no stages to print, no error to print, and the
    "your image is too old" advice never reached the operator — they got a bare
    "status: broken" with nothing to act on.
    """
    _route(monkeypatch, {"/health/pipeline": _Resp({"detail": "Not Found"}, status_code=404)})
    result = CliRunner().invoke(cli.main, ["check", "--pipeline-only"])
    assert result.exit_code == 1
    assert "predates" in result.output
    assert "HTTP 404" in result.output


def test_a_markup_shaped_hint_does_not_crash_the_report(isolated, monkeypatch):
    """A closing tag with no opener raises MarkupError, killing the command
    mid-render — the operator loses the whole report to a stray bracket."""
    stages = {"drain": {"status": "fail", "hint": "[/red]INJECTED[bold red]"}}
    _route(monkeypatch, {"/health/pipeline": _Resp(_pipeline("broken", stages))})
    result = CliRunner().invoke(cli.main, ["check", "--pipeline-only"])
    assert result.exit_code == 1, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "INJECTED" in result.output


def test_json_mode_emits_only_json(isolated, monkeypatch):
    """Anything piping this into jq breaks on a trailing status line."""
    _route(
        monkeypatch,
        {"/health/deep": _Resp(_DEPS_OK), "/health/pipeline": _Resp(_pipeline("degraded"))},
    )
    result = CliRunner().invoke(cli.main, ["check", "--json"])
    assert result.exit_code == 2
    parsed = json.loads(result.output)
    assert parsed["pipeline"]["status"] == "degraded"
    assert parsed["dependencies"]["status"] == "ok"


def test_json_omits_a_probe_that_was_not_run(isolated, monkeypatch):
    _route(monkeypatch, {"/health/pipeline": _Resp(_pipeline("ok"))})
    result = CliRunner().invoke(cli.main, ["check", "--pipeline-only", "--json"])
    parsed = json.loads(result.output)
    assert "pipeline" in parsed
    assert "dependencies" not in parsed


def test_the_two_scope_flags_are_mutually_exclusive(isolated):
    result = CliRunner().invoke(cli.main, ["check", "--deps-only", "--pipeline-only"])
    assert result.exit_code == 2


def test_square_brackets_from_the_server_survive_rendering(isolated, monkeypatch):
    """Rich reads `[...]` as a style tag. Severities, paths and tracebacks are
    full of brackets, and losing them loses the evidence — an unknown style
    can also raise, taking the whole report with it."""
    stages = {
        "signals": {
            "status": "fail",
            "broadcast_by_severity": {"[nuclear]": 1},
            "error_tail": 'File "/app/x.py", line 9, in <module>[not-a-style]',
            "hint": "expired [nuclear] alert still broadcasting",
        }
    }
    _route(monkeypatch, {"/health/pipeline": _Resp(_pipeline("broken", stages))})
    result = CliRunner().invoke(cli.main, ["check", "--pipeline-only"])
    assert result.exit_code == 1
    assert "[nuclear]" in result.output
    assert "not-a-style" in result.output


def test_buffered_markers_are_reported_because_the_server_cannot_see_them(
    isolated, monkeypatch, tmp_path
):
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    (outbox / "a.json").write_text("{}")
    monkeypatch.setenv("BRAIN_WRITER_OUTBOX", str(outbox))
    _route(monkeypatch, {"/health/pipeline": _Resp(_pipeline("ok"))})

    result = CliRunner().invoke(cli.main, ["check", "--pipeline-only"])

    assert "outbox=1" in result.output
    assert "agentibrain sync" in result.output


# ---------------------------------------------------------------------------
# _resolve_token — shared by check, tick and sync, so a regression here is
# three regressions.
# ---------------------------------------------------------------------------


def _settings(tmp_path: Path):
    s = cli.BrainSettings(_env_file=None)
    object.__setattr__(s, "config_dir", tmp_path)
    return s


def test_an_explicit_token_wins(tmp_path):
    assert cli._resolve_token(_settings(tmp_path), "flag-token") == "flag-token"


def test_a_token_is_read_from_the_deployment_env_file(tmp_path):
    (tmp_path / ".env").write_text("OTHER=1\nKB_ROUTER_TOKEN=from-file\n")
    assert cli._resolve_token(_settings(tmp_path), None) == "from-file"


def test_an_empty_env_line_is_not_a_token(tmp_path):
    """Parity with the inline blocks this helper replaced: an empty value is
    no token, and exits rather than sending a bare `Bearer ` that 401s two
    layers from its cause."""
    (tmp_path / ".env").write_text("KB_ROUTER_TOKEN=\n")
    with pytest.raises(SystemExit) as e:
        cli._resolve_token(_settings(tmp_path), None)
    assert e.value.code == 2


def test_a_later_populated_line_wins_over_an_earlier_empty_one(tmp_path):
    """A deliberate divergence from the inline blocks, which stopped at the
    first matching line and exited even when a real value followed."""
    (tmp_path / ".env").write_text("KB_ROUTER_TOKEN=\nKB_ROUTER_TOKEN=real\n")
    assert cli._resolve_token(_settings(tmp_path), None) == "real"


def test_no_token_anywhere_exits_two(tmp_path):
    with pytest.raises(SystemExit) as e:
        cli._resolve_token(_settings(tmp_path), None)
    assert e.value.code == 2
