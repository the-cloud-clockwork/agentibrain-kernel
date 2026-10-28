"""net_wait — bounded retry before an ops job's first call."""

from __future__ import annotations

import importlib
import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import net_wait  # noqa: E402


@pytest.fixture
def unlistened():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    yield sock
    sock.close()


def test_endpoint_refusing_first_attempts_still_succeeds(unlistened):
    port = unlistened.getsockname()[1]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            unlistened.listen()

    attempt = net_wait.wait_for(f"http://127.0.0.1:{port}/embed", attempts=5, sleep=sleep)

    assert attempt == 3
    assert sleeps == [1.0, 2.0]


def test_endpoint_still_refusing_gives_up_after_bound(unlistened):
    port = unlistened.getsockname()[1]
    sleeps = []

    with pytest.raises(net_wait.NetworkNotReady) as exc:
        net_wait.wait_for(f"http://127.0.0.1:{port}", attempts=4, sleep=sleeps.append)

    assert sleeps == [1.0, 2.0, 4.0]
    assert f"http://127.0.0.1:{port} unreachable after 4 attempts" in str(exc.value)


def test_backoff_is_capped():
    sleeps = []

    def refuse(address, timeout):
        raise TimeoutError("timed out")

    with pytest.raises(net_wait.NetworkNotReady):
        net_wait.wait_for("https://embeddings", attempts=6, sleep=sleeps.append, connect=refuse)

    assert sleeps == [1.0, 2.0, 4.0, 8.0, 8.0]


def test_default_port_follows_scheme():
    seen = []

    def connect(address, timeout):
        seen.append(address)
        return socket.socket()

    net_wait.wait_for("https://embeddings/embed", connect=connect)
    net_wait.wait_for("http://brain-api", connect=connect)

    assert seen == [("embeddings", 443), ("brain-api", 80)]


@pytest.mark.parametrize("script", ["embed_arcs", "embed_raw"])
def test_embed_job_fails_loudly_before_its_first_call(script, tmp_path, monkeypatch, capsys):
    module = importlib.import_module(script)
    calls = []

    def unreachable(url, **_k):
        calls.append(url)
        raise net_wait.NetworkNotReady(f"{url} unreachable after 6 attempts: refused")

    monkeypatch.setattr(net_wait, "wait_for", unreachable)
    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *_a, **_k: calls.append("call"))
    monkeypatch.setattr(
        sys,
        "argv",
        [script, "--vault", str(tmp_path), "--api-url", "http://embeddings:8080", "--api-key", "k"],
    )

    with pytest.raises(SystemExit) as exit_info:
        module.main()

    assert exit_info.value.code == 1
    assert calls == ["http://embeddings:8080"]
    assert "ERROR: http://embeddings:8080 unreachable after 6 attempts" in capsys.readouterr().err


def _tick_argv(tmp_path, *extra):
    return ["brain_tick", "--vault", str(tmp_path), "--brain-feed", str(tmp_path / "feed"), *extra]


def test_tick_fails_loudly_before_its_first_inference_call(tmp_path, monkeypatch, capsys):
    import brain_tick

    calls = []

    def unreachable(url, **_k):
        calls.append(url)
        raise net_wait.NetworkNotReady(f"{url} unreachable after 6 attempts: refused")

    monkeypatch.setattr(net_wait, "wait_for", unreachable)
    monkeypatch.setattr(brain_tick, "run_tick", lambda *_a, **_k: calls.append("tick"))
    monkeypatch.setattr(
        sys, "argv", _tick_argv(tmp_path, "--inference-url", "http://inference:4000")
    )

    with pytest.raises(SystemExit) as exit_info:
        brain_tick.main()

    assert exit_info.value.code == 1
    assert calls == ["http://inference:4000"]
    assert "ERROR: http://inference:4000 unreachable after 6 attempts" in capsys.readouterr().err


@pytest.mark.parametrize(
    "extra",
    [
        ("--inference-url", ""),
        ("--inference-url", "http://inference:4000", "--no-ai"),
        ("--inference-url", "http://inference:4000", "--dry-run"),
    ],
)
def test_tick_without_ai_skips_the_wait(extra, tmp_path, monkeypatch):
    import brain_tick

    calls = []
    monkeypatch.setattr(net_wait, "wait_for", lambda url, **_k: calls.append(url))
    monkeypatch.setattr(brain_tick, "run_tick", lambda *_a, **_k: {"phases": {}})
    monkeypatch.setattr(sys, "argv", _tick_argv(tmp_path, *extra))

    assert brain_tick.main() == 0
    assert calls == []
