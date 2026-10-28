"""net_wait — bounded retry before an ops job's first call."""

from __future__ import annotations

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
