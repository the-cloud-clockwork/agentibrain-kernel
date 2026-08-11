"""normalize_redis_db must land on the event-bus DB from any legal REDIS_URL.

The amygdala is handed whatever REDIS_URL the surrounding environment uses,
which is nearly always the shared cache on DB 0. Pointing it at the wrong
database is the worst kind of failure this service has: it connects, finds no
streams, and emits a healthy-looking "no signals" heartbeat forever.

The shell form this replaced — ``${url%/*}/11`` — passes the common cases and
silently destroys a URL with no explicit database, which is legal and means
DB 0. ``redis://host:6379`` became ``redis://11``: a connection to a host
named "11".
"""

from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "amygdala.py"


def _load_normalize():
    """Load just the function, without importing redis at module scope."""
    src = _SRC.read_text()
    start = src.index("def normalize_redis_db")
    end = src.index("def _redact_redis_url")
    ns: dict = {}
    exec(compile(src[start:end], str(_SRC), "exec"), ns)  # noqa: S102
    return ns["normalize_redis_db"]


normalize_redis_db = _load_normalize()


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # The everyday case: a cache URL redirected onto the bus.
        ("redis://:pass@10.10.30.130:6379/0", "redis://:pass@10.10.30.130:6379/11"),
        ("redis://user:pass@host:6379/0", "redis://user:pass@host:6379/11"),
        ("rediss://:pass@host:6380/2", "rediss://:pass@host:6380/11"),
        ("redis://[::1]:6379/0", "redis://[::1]:6379/11"),
        # No explicit database — legal, means 0. The shell form ate the host here.
        ("redis://host:6379", "redis://host:6379/11"),
        ("redis://host", "redis://host/11"),
        ("redis://host:6379/", "redis://host:6379/11"),
        # Already correct: must be a no-op, not a double-append.
        ("redis://redis:6379/11", "redis://redis:6379/11"),
    ],
)
def test_lands_on_the_bus_database(url, expected):
    assert normalize_redis_db(url, 11) == expected


def test_host_survives_when_no_database_is_named():
    """The specific regression: the host must never be mistaken for a DB number."""
    assert "11" not in normalize_redis_db("redis://host:6379", 11).split("//")[1].split("/")[0]


def test_accepts_db_as_str_or_int():
    assert normalize_redis_db("redis://h/0", "11") == normalize_redis_db("redis://h/0", 11)
