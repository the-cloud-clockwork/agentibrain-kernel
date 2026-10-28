"""Wait out a new pod's network policy before an ops job's first call."""

from __future__ import annotations

import socket
import sys
import time
import urllib.parse
from collections.abc import Callable

ATTEMPTS = 6
FIRST_DELAY = 1.0
MAX_DELAY = 8.0
CONNECT_TIMEOUT = 3.0


class NetworkNotReady(RuntimeError):
    pass


def wait_for(
    url: str,
    attempts: int = ATTEMPTS,
    sleep: Callable[[float], object] = time.sleep,
    connect: Callable[..., socket.socket] = socket.create_connection,
) -> int:
    parts = urllib.parse.urlsplit(url)
    address = (parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
    for attempt in range(1, attempts + 1):
        try:
            connect(address, timeout=CONNECT_TIMEOUT).close()
            return attempt
        except OSError as exc:
            error = exc
        if attempt < attempts:
            sleep(min(FIRST_DELAY * 2 ** (attempt - 1), MAX_DELAY))
    raise NetworkNotReady(f"{url} unreachable after {attempts} attempts: {error}")


def require(url: str, skip: bool = False) -> None:
    if skip:
        return
    try:
        wait_for(url)
    except NetworkNotReady as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
