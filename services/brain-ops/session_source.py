"""Normalized session change feed consumed by kernel extraction.

The feed is ``GET /v2/sessions/changes`` plus the exact ``GET /v2/sessions/{uid}/records`` ranges
its changes name, read with the extraction principal's project grant, so Claude, Codex and Copilot
work reaches the kernel through one source API. Evidence outside the grant, including unknown
attribution, refuses the whole batch before any state changes. A batch is staged with its output
and its cursor advances only through ``Store.commit`` once that output was accepted; a batch that
never commits is rebuilt from the last committed cursor.
"""

from __future__ import annotations

import copy
import fcntl
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from extract import COMPACT_MARKERS, ERROR_MARKERS, JOY_MARKERS, parse_ts

SCHEMA_VERSION = 1
LAG_METRIC = "brain_source_cursor_lag"
HARNESSES = frozenset({"claude", "codex", "copilot"})
RECORD_KINDS = frozenset({"user", "assistant", "tool_call", "tool_result", "injected"})
PAGE_FIELDS = frozenset({"schema_version", "changes", "next_cursor", "position", "head"})
RECORDS_FIELDS = frozenset({"schema_version", "session_uid", "records"})
RANGE_FIELDS = ("change_id", "kind", "session_uid", "source_generation", "first_sequence")
CHANGE_FIELDS = {
    "appended": frozenset({*RANGE_FIELDS, "last_sequence", "harness"}),
    "attribution": frozenset({*RANGE_FIELDS, "last_sequence", "project_id"}),
    "deleted": frozenset({"change_id", "kind", "session_uid"}),
}
RECORD_FIELDS = frozenset(
    {"sequence", "kind", "timestamp", "text", "tool", "project_id", "cwd", "git_branch"}
)
OUT_OF_SCOPE = "the feed returned evidence outside this principal's project grant; nothing changed"
NO_GRANT = "the extraction principal has no project grant"
NOTHING_STAGED = "no staged extraction batch to commit"
STALE_BATCH = "the committed cursor moved since this batch was built; rebuild it"
FEED_FAILED = "the session feed failed before answering; the same request can be retried"


class Refused(Exception):
    def __init__(self, error_class: str, detail: str) -> None:
        super().__init__(f"{error_class}: {detail}")
        self.error_class = error_class
        self.detail = detail


@dataclass(frozen=True)
class Grant:
    principal: str
    projects: frozenset[str]

    @classmethod
    def from_env(cls, environ: dict) -> Grant:
        projects = frozenset(p.strip() for p in environ.get("EXTRACT_PROJECTS", "").split(","))
        return cls(environ.get("EXTRACT_PRINCIPAL", ""), projects - {""})

    def require(self) -> None:
        if not self.principal or not self.projects:
            raise Refused("forbidden_scope", NO_GRANT)

    def allows(self, project_id: object) -> bool:
        return isinstance(project_id, str) and project_id in self.projects


class Source(Protocol):
    def changes(self, cursor: str | None, limit: int) -> dict: ...

    def records(self, change: dict, first: int) -> dict: ...


def _invalid(detail: str) -> Refused:
    return Refused("invalid_request", detail)


def _fields(value: object, allowed: frozenset, name: str) -> dict:
    if not isinstance(value, dict):
        raise _invalid(f"{name} is not an object")
    unknown = set(value) - allowed
    if unknown:
        raise _invalid(f"{name} has unsupported fields: {', '.join(sorted(unknown))}")
    return value


def _sequence(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise _invalid(f"{name} is not a positive sequence")
    return value


def _version(body: dict, name: str) -> None:
    if body.get("schema_version") != SCHEMA_VERSION:
        raise _invalid(f"{name} schema_version is not {SCHEMA_VERSION}")


def check_change(change: object) -> dict:
    kind = change.get("kind") if isinstance(change, dict) else None
    if kind not in CHANGE_FIELDS:
        raise _invalid("change kind is not supported")
    _fields(change, CHANGE_FIELDS[kind], "change")
    missing = CHANGE_FIELDS[kind] - set(change)
    if missing:
        raise _invalid(f"change lacks {', '.join(sorted(missing))}")
    if kind == "appended" and change["harness"] not in HARNESSES:
        raise _invalid("change harness is not supported")
    if kind != "deleted":
        first = _sequence(change["first_sequence"], "first_sequence")
        if _sequence(change["last_sequence"], "last_sequence") < first:
            raise _invalid("change range is reversed")
    return change


def check_page(page: object) -> dict:
    _fields(page, PAGE_FIELDS, "page")
    _version(page, "page")
    if not isinstance(page.get("changes"), list):
        raise _invalid("page changes is not a list")
    for name in ("position", "head"):
        if not isinstance(page.get(name), int) or page[name] < 0:
            raise _invalid(f"page {name} is not a feed position")
    if not isinstance(page.get("next_cursor"), str):
        raise _invalid("page next_cursor is not a cursor")
    for change in page["changes"]:
        check_change(change)
    return page


def check_records(body: object, change: dict, first: int) -> list[dict]:
    _fields(body, RECORDS_FIELDS, "records")
    _version(body, "records")
    if body.get("session_uid") != change["session_uid"] or not isinstance(body["records"], list):
        raise _invalid("records do not belong to the requested session")
    expected = list(range(first, change["last_sequence"] + 1))
    records = [_fields(record, RECORD_FIELDS, "record") for record in body["records"]]
    if [record.get("sequence") for record in records] != expected:
        raise _invalid("records do not cover the requested range exactly")
    if any(record.get("kind") not in RECORD_KINDS for record in records):
        raise _invalid("record kind is not supported")
    return records


class FeedSource:
    def __init__(self, base_url: str, token: str, grant: Grant, timeout: float = 30.0) -> None:
        grant.require()
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.grant = grant
        self.timeout = timeout

    def _get(self, path: str, params: dict) -> dict:
        query = {
            "schema_version": SCHEMA_VERSION,
            "principal": self.grant.principal,
            "projects": ",".join(sorted(self.grant.projects)),
            **params,
        }
        request = urllib.request.Request(
            f"{self.base_url}{path}?{urllib.parse.urlencode(query)}",
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                kind = "unauthenticated" if error.code == 401 else "forbidden_scope"
                raise Refused(kind, "the session feed refused this principal") from None
            raise Refused("dependency_unavailable", FEED_FAILED) from None
        except (OSError, ValueError):
            raise Refused("dependency_unavailable", FEED_FAILED) from None

    def changes(self, cursor: str | None, limit: int) -> dict:
        params = {"limit": limit, **({"cursor": cursor} if cursor else {})}
        return self._get("/v2/sessions/changes", params)

    def records(self, change: dict, first: int) -> dict:
        uid = urllib.parse.quote(change["session_uid"], safe="")
        params = {
            "source_generation": change["source_generation"],
            "first_sequence": first,
            "last_sequence": change["last_sequence"],
        }
        return self._get(f"/v2/sessions/{uid}/records", params)


def empty_state() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "revision": 0,
        "cursor": None,
        "position": 0,
        "sessions": {},
        "deleted": [],
    }


def _new_session(change: dict) -> dict:
    return {
        "harness": change["harness"],
        "source_generation": change["source_generation"],
        "applied": 0,
        "first_ts": None,
        "last_ts": None,
        "user": 0,
        "assistant": 0,
        "tools": {},
        "first_prompt": "",
        "last_prompt": "",
        "joy": 0,
        "errors": 0,
        "compactions": 0,
        "spans": [],
        "cwd": "",
        "git_branch": "",
    }


def _text(record: dict) -> str:
    return record["text"] if isinstance(record.get("text"), str) else ""


def _fold(session: dict, record: dict) -> None:
    timestamp = parse_ts(record.get("timestamp"))
    if timestamp is not None:
        first, last = parse_ts(session["first_ts"]), parse_ts(session["last_ts"])
        if first is None or timestamp < first:
            session["first_ts"] = timestamp.isoformat()
        if last is None or timestamp > last:
            session["last_ts"] = timestamp.isoformat()
    session["cwd"] = record.get("cwd") or session["cwd"]
    session["git_branch"] = record.get("git_branch") or session["git_branch"]
    text, kind = _text(record), record["kind"]
    if kind == "user" and text:
        session["user"] += 1
        session["first_prompt"] = session["first_prompt"] or text[:300]
        session["last_prompt"] = text[:300]
        session["compactions"] += bool(COMPACT_MARKERS.search(text))
    elif kind == "assistant":
        session["assistant"] += 1
        session["joy"] += session["joy"] < 50 and bool(JOY_MARKERS.search(text))
        session["errors"] += session["errors"] < 50 and bool(ERROR_MARKERS.search(text))
    elif kind == "tool_call":
        name = record.get("tool") or "unknown"
        session["tools"][name] = session["tools"].get(name, 0) + 1
    session["spans"] = respan(
        session["spans"], record["sequence"], record["sequence"], record["project_id"]
    )
    session["applied"] = record["sequence"]


def respan(spans: list, first: int, last: int, project_id: str) -> list:
    kept = [[a, min(b, first - 1), p] for a, b, p in spans if a < first]
    kept += [[max(a, last + 1), b, p] for a, b, p in spans if b > last]
    merged: list = []
    for a, b, p in sorted([*kept, [first, last, project_id]]):
        if merged and merged[-1][2] == p and merged[-1][1] + 1 == a:
            merged[-1][1] = b
        else:
            merged.append([a, b, p])
    return merged


class Extraction:
    def __init__(self, source: Source, grant: Grant) -> None:
        grant.require()
        self.source = source
        self.grant = grant

    def _scope(self, project_id: object) -> None:
        if not self.grant.allows(project_id):
            raise Refused("forbidden_scope", OUT_OF_SCOPE)

    def _appended(self, state: dict, change: dict, touched: set) -> None:
        uid = change["session_uid"]
        session = state["sessions"].get(uid)
        if session is None or session["source_generation"] != change["source_generation"]:
            session = _new_session(change)
        first = max(change["first_sequence"], session["applied"] + 1)
        if first > change["last_sequence"]:
            return
        records = check_records(self.source.records(change, first), change, first)
        for record in records:
            self._scope(record.get("project_id"))
        for record in records:
            _fold(session, record)
        state["sessions"][uid] = session
        touched.add(uid)

    def _attribution(self, state: dict, change: dict, touched: set) -> None:
        self._scope(change["project_id"])
        session = state["sessions"].get(change["session_uid"])
        if session is None or session["source_generation"] != change["source_generation"]:
            return
        last = min(change["last_sequence"], session["applied"])
        if change["first_sequence"] <= last:
            session["spans"] = respan(
                session["spans"], change["first_sequence"], last, change["project_id"]
            )
            touched.add(change["session_uid"])

    def apply(self, state: dict, page: dict, touched: set) -> None:
        for change in check_page(page)["changes"]:
            uid = change["session_uid"]
            if uid in state["deleted"]:
                continue
            if change["kind"] == "deleted":
                state["sessions"].pop(uid, None)
                state["deleted"].append(uid)
                touched.discard(uid)
            elif change["kind"] == "appended":
                self._appended(state, change, touched)
            else:
                self._attribution(state, change, touched)
        state["cursor"] = page["next_cursor"]
        state["position"] = page["position"]

    def build(self, committed: dict, limit: int, max_pages: int) -> tuple[dict, set, int]:
        state, touched, head = copy.deepcopy(committed), set(), committed["position"]
        for _ in range(max_pages):
            page = self.source.changes(state["cursor"], limit)
            self.apply(state, page, touched)
            head = page["head"]
            if not page["changes"] or state["position"] >= head:
                break
        return state, touched, head


def summary(uid: str, session: dict) -> dict:
    spans = session["spans"]
    project_id = spans[-1][2] if spans else None
    tools = Counter(session["tools"])
    start, end = session["first_ts"], session["last_ts"]
    return {
        "session_id": uid,
        "project": project_id.rsplit("/", 1)[-1] if project_id else "unknown",
        "project_id": project_id,
        "project_ids": sorted({p for _, _, p in spans}),
        "worktree": "",
        "cwd": session["cwd"],
        "harness": session["harness"],
        "git_branch": session["git_branch"],
        "start": start,
        "end": end,
        "duration_minutes": _minutes(start, end),
        "num_user_turns": session["user"],
        "num_assistant_turns": session["assistant"],
        "num_tool_calls": sum(tools.values()),
        "first_user_prompt": session["first_prompt"],
        "last_user_prompt": session["last_prompt"],
        "top_tools": [{"name": n, "count": c} for n, c in tools.most_common(10)],
        "compactions": session["compactions"],
        "joy_markers": session["joy"],
        "error_markers": session["errors"],
        "source": "session-feed",
    }


def _minutes(start: str | None, end: str | None) -> float:
    first, last = parse_ts(start), parse_ts(end)
    return round((last - first).total_seconds() / 60.0, 1) if first and last else 0.0


class Store:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.committed_path = self.root / "committed.json"
        self.staged_path = self.root / "staged.json"

    @contextmanager
    def _locked(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self.root / ".lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def _write(self, path: Path, body: dict) -> None:
        temporary = path.with_suffix(".tmp")
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(body, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        directory = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def load(self) -> dict:
        if not self.committed_path.exists():
            return empty_state()
        state = json.loads(self.committed_path.read_text(encoding="utf-8"))
        _version(state, "committed state")
        return state

    def stage(self, state: dict, base_revision: int, stage_id: str) -> str:
        with self._locked():
            if self.load()["revision"] != base_revision:
                raise Refused("revision_conflict", STALE_BATCH)
            self._write(
                self.staged_path,
                {"stage_id": stage_id, "base_revision": base_revision, "state": state},
            )
            return stage_id

    def commit(self, stage_id: str) -> dict:
        with self._locked():
            if not self.staged_path.exists():
                raise Refused("revision_conflict", NOTHING_STAGED)
            staged = json.loads(self.staged_path.read_text(encoding="utf-8"))
            if staged["stage_id"] != stage_id:
                raise Refused("revision_conflict", STALE_BATCH)
            if self.load()["revision"] != staged["base_revision"]:
                self.staged_path.unlink()
                raise Refused("revision_conflict", STALE_BATCH)
            state = {**staged["state"], "revision": staged["base_revision"] + 1}
            self._write(self.committed_path, state)
            self.staged_path.unlink()
            return state


def extract_batch(
    source: Source,
    grant: Grant,
    store: Store,
    min_turns: int,
    limit: int = 100,
    max_pages: int = 50,
    stage_id: str = "",
) -> dict:
    committed = store.load()
    state, touched, head = Extraction(source, grant).build(committed, limit, max_pages)
    stage_id = store.stage(state, committed["revision"], stage_id or uuid.uuid4().hex)
    sessions = [summary(uid, state["sessions"][uid]) for uid in sorted(touched)]
    returned = [s for s in sessions if s["num_user_turns"] >= min_turns]
    returned.sort(key=lambda s: (s["start"] or "", s["session_id"]))
    return {
        "source": {
            "kind": "session-feed",
            "principal": grant.principal,
            "stage_id": stage_id,
            "committed_position": committed["position"],
            "staged_position": state["position"],
            "head": head,
            LAG_METRIC: {
                "committed": head - committed["position"],
                "after_commit": head - state["position"],
            },
        },
        "filter": {"projects": sorted(grant.projects), "min_turns": min_turns},
        "deleted_sessions": sorted(set(state["deleted"]) - set(committed["deleted"])),
        "sessions_scanned": len(sessions),
        "sessions_returned": len(returned),
        "sessions": returned,
    }


def feed_from_env(environ: dict) -> tuple[FeedSource, Grant]:
    grant = Grant.from_env(environ)
    token = environ.get(environ.get("SESSION_FEED_TOKEN_ENV") or "SESSION_FEED_TOKEN", "")
    url = environ.get("SESSION_FEED_URL", "")
    if not url or not token:
        raise Refused("unauthenticated", "SESSION_FEED_URL and the feed token must be set")
    return FeedSource(url, token, grant), grant
