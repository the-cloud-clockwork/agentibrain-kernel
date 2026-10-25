"""SV2-ING-01: kernel extraction consumes the normalized session change feed.

The fixture is two harnesses, a changed attribution, a deleted source and an
interrupted extraction batch, served over HTTP by a fixture-local feed. These
tests are mocked evidence: the feed and grant are fixture-local, not production.

Run from repo root:
    pytest -q services/brain-ops/tests/test_session_source.py
"""

from __future__ import annotations

import copy
import importlib
import json
import os
import subprocess
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_BRAIN_OPS = _HERE.parent
if str(_BRAIN_OPS) not in sys.path:
    sys.path.insert(0, str(_BRAIN_OPS))

session_source = importlib.import_module("session_source")
cluster_module = importlib.import_module("cluster")

FIXTURE = json.loads((_HERE / "fixtures" / "sv2_ing01_feed.json").read_text(encoding="utf-8"))
TOKEN = "fixture-feed-token"
C1, X1, C2, X9 = "claude:hive-a:c1", "codex:hive-a:x1", "claude:hive-a:c2", "codex:hive-a:x9"
ALPHA, GAMMA = "github.com/acme/alpha", "github.com/acme/gamma"


def independent(fixture: dict, prefix: str) -> dict:
    """A second fixture whose session ids and principal share nothing with the first."""
    text = json.dumps(fixture).replace("hive-a", prefix)
    return json.loads(text)


class Feed:
    def __init__(self, fixture: dict) -> None:
        self.fixture = copy.deepcopy(fixture)
        self.changes = list(self.fixture["changes"])
        self.requests: list[tuple[str, dict]] = []
        self.status = 200
        self.page_override: dict = {}

    def leak(self) -> None:
        self.changes.insert(2, self.fixture["outside_change"])

    def _visible(self, change: dict, projects: set[str]) -> bool:
        records = self.fixture["sessions"][change["session_uid"]]["records"]
        return all(r["project_id"] in projects for r in records)

    def page(self, query: dict) -> dict:
        projects = set(query["projects"].split(","))
        position = int(query.get("cursor") or 0)
        limit = int(query["limit"])
        chunk = self.changes[position : position + limit]
        honest = [c for c in chunk if c["kind"] == "deleted" or self._visible(c, projects)]
        leaked = [c for c in chunk if c["session_uid"] == X9]
        position += len(chunk)
        return {
            "schema_version": 1,
            "changes": honest + [c for c in leaked if c not in honest],
            "next_cursor": str(position),
            "position": position,
            "head": len(self.changes),
            **self.page_override,
        }

    def records(self, uid: str, query: dict) -> dict:
        first, last = int(query["first_sequence"]), int(query["last_sequence"])
        held = self.fixture["sessions"][uid]["records"]
        return {
            "schema_version": 1,
            "session_uid": uid,
            "records": [r for r in held if first <= r["sequence"] <= last],
        }


@pytest.fixture
def serve():
    servers = []

    def start(feed: Feed) -> str:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                parsed = urllib.parse.urlparse(self.path)
                query = dict(urllib.parse.parse_qsl(parsed.query))
                feed.requests.append((parsed.path, query))
                if self.headers.get("Authorization") != f"Bearer {TOKEN}" or feed.status != 200:
                    self.send_response(401 if feed.status == 200 else feed.status)
                    self.end_headers()
                    return
                if parsed.path == "/v2/sessions/changes":
                    body = feed.page(query)
                else:
                    uid = urllib.parse.unquote(parsed.path.split("/")[3])
                    body = feed.records(uid, query)
                payload = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_port}"

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def grant_of(fixture: dict) -> session_source.Grant:
    return session_source.Grant(
        fixture["grant"]["principal"], frozenset(fixture["grant"]["projects"])
    )


def source_for(url: str, fixture: dict) -> session_source.FeedSource:
    return session_source.FeedSource(url, TOKEN, grant_of(fixture))


def run(url: str, fixture: dict, store, **kwargs) -> dict:
    return session_source.extract_batch(
        source_for(url, fixture), grant_of(fixture), store, min_turns=1, **kwargs
    )


def by_id(bundle: dict) -> dict:
    return {s["session_id"]: s for s in bundle["sessions"]}


def committed_bytes(store) -> bytes | None:
    return store.committed_path.read_bytes() if store.committed_path.exists() else None


def cluster(bundle: dict, out_dir: Path, tmp_path: Path) -> None:
    path = tmp_path / f"bundle-{len(list(tmp_path.glob('bundle-*')))}.json"
    path.write_text(json.dumps(bundle), encoding="utf-8")
    subprocess.run(
        [
            sys.executable,
            str(_BRAIN_OPS / "cluster.py"),
            "--bundle",
            str(path),
            "--out-dir",
            str(out_dir),
        ],
        check=True,
        capture_output=True,
    )


def stub_sessions(out_dir: Path) -> list[str]:
    found = []
    for stub in out_dir.glob("*.md"):
        if stub.name.startswith("_"):
            continue
        found += sorted(cluster_module.recorded_sessions(stub))
    return sorted(found)


@pytest.mark.parametrize("prefix", ["hive-a", "hive-b"])
def test_two_harnesses_extract_through_one_feed(serve, tmp_path: Path, prefix: str) -> None:
    fixture = independent(FIXTURE, prefix)
    c1, x1, c2 = (uid.replace("hive-a", prefix) for uid in (C1, X1, C2))
    url = serve(Feed(fixture))
    store = session_source.Store(tmp_path / "state")

    bundle = run(url, fixture, store, limit=3)
    assert store.committed_path.exists() is False
    committed = store.commit(bundle["source"]["stage_id"])

    sessions = by_id(bundle)
    assert set(sessions) == {c1, x1}
    assert {s["harness"] for s in sessions.values()} == {"claude", "codex"}
    assert sessions[c1]["num_user_turns"] == 2
    assert sessions[c1]["first_user_prompt"] == "fix the failing ledger write"
    assert sessions[c1]["num_tool_calls"] == 1
    assert sessions[c1]["project_id"] == ALPHA
    assert sessions[x1]["project_id"] == GAMMA
    assert sessions[x1]["project_ids"] == [GAMMA]
    assert bundle["deleted_sessions"] == [c2]
    assert committed["position"] == bundle["source"]["head"] == len(fixture["changes"])
    assert bundle["source"]["brain_source_cursor_lag"] == {"committed": 8, "after_commit": 0}

    stored = json.loads(store.committed_path.read_text())
    assert set(stored["sessions"]) == {c1, x1}
    assert stored["deleted"] == [c2]
    assert stored["sessions"][x1]["spans"] == [[1, 4, GAMMA]]
    assert stored["revision"] == 1


def test_replayed_change_does_not_refetch_or_recount(serve, tmp_path: Path) -> None:
    feed = Feed(FIXTURE)
    url = serve(feed)
    store = session_source.Store(tmp_path / "state")
    run(url, FIXTURE, store, limit=8)
    record_requests = [q for path, q in feed.requests if path != "/v2/sessions/changes"]
    assert [(q["first_sequence"], q["last_sequence"]) for q in record_requests] == [
        ("1", "3"),
        ("1", "2"),
        ("1", "2"),
        ("4", "6"),
        ("3", "4"),
    ]


def test_bundle_clusters_each_session_once(serve, tmp_path: Path) -> None:
    url = serve(Feed(FIXTURE))
    bundle = run(url, FIXTURE, session_source.Store(tmp_path / "state"))
    out_dir = tmp_path / "clusters" / "2026-10-20"
    cluster(bundle, out_dir, tmp_path)
    assert stub_sessions(out_dir) == sorted([C1, X1])


def test_cli_stages_then_commits_after_acceptance(serve, tmp_path: Path) -> None:
    url = serve(Feed(FIXTURE))
    env = {
        **os.environ,
        "SESSION_FEED_URL": url,
        "SESSION_FEED_TOKEN": TOKEN,
        "EXTRACT_PRINCIPAL": FIXTURE["grant"]["principal"],
        "EXTRACT_PROJECTS": ",".join(FIXTURE["grant"]["projects"]),
    }
    state = tmp_path / "state"
    base = [sys.executable, str(_BRAIN_OPS / "extract.py"), "--source", "feed"]
    staged = subprocess.run(
        [*base, "--state-dir", str(state), "--min-turns", "1"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert staged.returncode == 0, staged.stderr
    assert "brain_source_cursor_lag=0" in staged.stderr
    assert {s["session_id"] for s in json.loads(staged.stdout)["sessions"]} == {C1, X1}
    assert not (state / "committed.json").exists()

    done = subprocess.run(
        [*base, "--state-dir", str(state), "--commit"], env=env, capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr
    assert json.loads((state / "committed.json").read_text())["position"] == 8


def _seed(url: str, store) -> bytes:
    bundle = run(url, FIXTURE, store, limit=2, max_pages=1)
    store.commit(bundle["source"]["stage_id"])
    return committed_bytes(store)


def test_out_of_grant_change_refuses_the_batch(serve, tmp_path: Path) -> None:
    feed = Feed(FIXTURE)
    url = serve(feed)
    store = session_source.Store(tmp_path / "state")
    before = _seed(url, store)
    feed.leak()

    with pytest.raises(session_source.Refused) as refused:
        run(url, FIXTURE, store)

    assert refused.value.error_class == "forbidden_scope"
    assert "beta" not in str(refused.value) and X9 not in str(refused.value)
    assert committed_bytes(store) == before
    assert not store.staged_path.exists()


def test_attribution_outside_grant_refuses_the_batch(serve, tmp_path: Path) -> None:
    fixture = copy.deepcopy(FIXTURE)
    fixture["changes"][4]["project_id"] = FIXTURE["outside_project"]
    url = serve(Feed(fixture))
    store = session_source.Store(tmp_path / "state")
    with pytest.raises(session_source.Refused) as refused:
        run(url, fixture, store)
    assert refused.value.error_class == "forbidden_scope"
    assert committed_bytes(store) is None and not store.staged_path.exists()


def test_unknown_attribution_is_not_a_grant(serve, tmp_path: Path) -> None:
    fixture = copy.deepcopy(FIXTURE)
    fixture["sessions"][C1]["records"][0]["project_id"] = None
    fixture["grant"]["projects"].append("None")
    feed = Feed(fixture)
    feed._visible = lambda change, projects: True
    url = serve(feed)
    with pytest.raises(session_source.Refused) as refused:
        run(url, fixture, session_source.Store(tmp_path / "state"))
    assert refused.value.error_class == "forbidden_scope"


def test_missing_grant_refuses_before_any_request(serve, tmp_path: Path) -> None:
    feed = Feed(FIXTURE)
    url = serve(feed)
    with pytest.raises(session_source.Refused) as refused:
        session_source.FeedSource(
            url, TOKEN, session_source.Grant("brain-extractor@hive-a", frozenset())
        )
    assert refused.value.error_class == "forbidden_scope"
    assert feed.requests == []


@pytest.mark.parametrize(
    "override, detail",
    [
        ({"schema_version": 2}, "schema_version"),
        ({"grant_scope": "all"}, "unsupported fields: grant_scope"),
    ],
)
def test_unsupported_page_refuses_without_mutation(serve, tmp_path: Path, override, detail) -> None:
    feed = Feed(FIXTURE)
    url = serve(feed)
    store = session_source.Store(tmp_path / "state")
    before = _seed(url, store)
    feed.page_override = override
    with pytest.raises(session_source.Refused) as refused:
        run(url, FIXTURE, store)
    assert refused.value.error_class == "invalid_request"
    assert detail in refused.value.detail
    assert committed_bytes(store) == before


@pytest.mark.parametrize(
    "status, error_class", [(403, "forbidden_scope"), (503, "dependency_unavailable")]
)
def test_feed_refusal_has_no_local_fallback(serve, tmp_path: Path, status, error_class) -> None:
    feed = Feed(FIXTURE)
    feed.status = status
    url = serve(feed)
    store = session_source.Store(tmp_path / "state")
    with pytest.raises(session_source.Refused) as refused:
        run(url, FIXTURE, store)
    assert refused.value.error_class == error_class
    assert TOKEN not in str(refused.value)
    assert committed_bytes(store) is None and not store.staged_path.exists()


def test_interrupted_batch_resumes_from_committed_cursor_without_duplicate_arcs(
    serve, tmp_path: Path
) -> None:
    feed = Feed(FIXTURE)
    url = serve(feed)
    store = session_source.Store(tmp_path / "state")
    out_dir = tmp_path / "clusters" / "2026-10-20"
    first = run(url, FIXTURE, store, limit=3, max_pages=1)
    cluster(first, tmp_path / "clusters" / "2026-10-19", tmp_path)
    store.commit(first["source"]["stage_id"])
    committed_cursor = store.load()["cursor"]

    interrupted = run(url, FIXTURE, store, limit=3, max_pages=1)
    cluster(interrupted, out_dir, tmp_path)
    assert store.load()["cursor"] == committed_cursor

    feed.requests.clear()
    resumed = run(url, FIXTURE, store, limit=3)
    assert feed.requests[0][1]["cursor"] == committed_cursor
    cluster(resumed, out_dir, tmp_path)
    store.commit(resumed["source"]["stage_id"])

    clean_store = session_source.Store(tmp_path / "clean")
    clean = run(url, FIXTURE, clean_store, limit=3)
    clean_state = clean_store.commit(clean["source"]["stage_id"])
    resumed_state = store.load()
    assert resumed_state["sessions"] == clean_state["sessions"]
    assert resumed_state["deleted"] == clean_state["deleted"]
    assert resumed_state["position"] == clean_state["position"] == 8

    arcs = stub_sessions(out_dir)
    assert len(arcs) == len(set(arcs))
    assert by_id(resumed)[C1]["num_user_turns"] == by_id(clean)[C1]["num_user_turns"] == 2


def test_stale_attempt_cannot_overwrite_newer_commit(serve, tmp_path: Path) -> None:
    url = serve(Feed(FIXTURE))
    store = session_source.Store(tmp_path / "state")
    stale = run(url, FIXTURE, store, limit=2, max_pages=1)
    newer = run(url, FIXTURE, store)
    committed = store.commit(newer["source"]["stage_id"])
    snapshot = committed_bytes(store)

    with pytest.raises(session_source.Refused) as refused:
        store.commit(stale["source"]["stage_id"])
    assert refused.value.error_class == "revision_conflict"

    old_state = session_source.empty_state()
    with pytest.raises(session_source.Refused):
        store.stage(old_state, base_revision=0)
    assert committed_bytes(store) == snapshot
    assert committed["position"] == 8


def test_new_source_generation_replaces_the_replaced_source(serve, tmp_path: Path) -> None:
    fixture = copy.deepcopy(FIXTURE)
    fixture["changes"] = fixture["changes"][:2]
    feed = Feed(fixture)
    url = serve(feed)
    store = session_source.Store(tmp_path / "state")
    store.commit(run(url, fixture, store)["source"]["stage_id"])
    replaced = dict(
        fixture["changes"][0], change_id="ch-9", source_generation="g2", last_sequence=1
    )
    feed.changes.append(replaced)
    feed.fixture["sessions"][C1]["source_generation"] = "g2"
    bundle = run(url, fixture, store)
    assert by_id(bundle)[C1]["num_user_turns"] == 1
    assert store.commit(bundle["source"]["stage_id"])["sessions"][C1]["source_generation"] == "g2"
