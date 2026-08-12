"""Pipeline health — is the brain's loop actually flowing?

`/health/deep` answers "can every dependency be reached". This module answers
the different question: **is data moving through the loop**. A stack whose
dependencies all pass can still be dead — the drain not consuming, the tick
failing on every run, lessons written but never fed back, an index that holds
no rows for a producer whose source files exist.

Seven stages, in the order data travels:

    ingest → drain → arcs → lessons → signals → feed → index

Each returns ``{status, ...evidence, hint}`` where status is ``ok``, ``warn``
or ``fail``. Every stage carries the evidence its verdict rests on, because a
verdict a caller cannot audit is worth nothing.

**An unused brain is healthy.** A stage with no input reports ``ok`` with a
note, never ``fail`` — a fresh vault has no arcs, no lessons and no ticks, and
reporting that as breakage trains the operator to ignore the command. A stage
fails only when its *input exists* and its *output does not*.

This runs server-side because the vault is only mounted here: the same report
is therefore reachable from any machine that can reach brain-api, which is what
makes `agentibrain check` meaningful against a remote deployment.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .feed import BRAIN_FEED_DIR, VAULT_ROOT, read_feed
from .markers import _LESSON_ENTRY_HEADER_RE, _LESSON_ENTRY_SPLIT_RE

# Regions the tick can relocate a file into. Lesson-scatter detection walks
# these plus clusters/ rather than the whole vault: graduation and promotion
# are the only movers, and both target this set, so a bounded scan sees
# everything they can do while staying cheap on an NFS mount.
REGION_DIRS = ("bridge", "left", "right", "frontal-lobe", "pineal", "amygdala")
CLUSTERS_DIR = "clusters"

# Anchored and date-shaped, matching `services/brain-ops/markers.py::LESSON_LOG_RE`
# — the pattern the reconcile itself uses to decide what it owns. Duplicated
# rather than imported because brain-ops ships as a separate image; the two must
# be changed together. A loose `lessons-*` would flag a file merely named that
# way (`lessons-learned-notes.md`) as scatter, and no tick would ever clear it,
# because reconcile correctly refuses to touch a file that is not a lesson log.
LESSON_LOG_RE = re.compile(r"^lessons-\d{4}-\d{2}-\d{2}\.md$")

# Thresholds. Defaults track the shipped cadences: tick-drain polls every 30s,
# the brain-ops tick cron fires every 2h.
PENDING_STUCK_SECONDS = int(os.getenv("PIPELINE_PENDING_STUCK_SECONDS", "300"))
TICK_STALE_HOURS = int(os.getenv("PIPELINE_TICK_STALE_HOURS", "6"))
FEED_STALE_HOURS = int(os.getenv("PIPELINE_FEED_STALE_HOURS", "6"))
MARKER_QUIET_HOURS = int(os.getenv("PIPELINE_MARKER_QUIET_HOURS", "72"))
LESSON_FEED_MAX = int(os.getenv("BRAIN_LESSON_FEED_MAX", "6"))
LESSON_WINDOW_DAYS = int(os.getenv("BRAIN_STALE_LESSON_DAYS", "14"))
# Hard ceiling on filesystem work per request, so the cost of this endpoint
# does not grow with the vault. Counts feed threshold comparisons, not an
# inventory, so truncation changes nothing about the verdicts.
MAX_SCAN = int(os.getenv("PIPELINE_MAX_SCAN", "5000"))
# How far the AI-phase feeds may lag the deterministic ones before that gap is
# itself the finding. Generous, because these two are written only when the
# reasoning phase has something to say — the signal is a chasm, not a wobble.
AI_PHASE_LAG_HOURS = int(os.getenv("PIPELINE_AI_PHASE_LAG_HOURS", "24"))

# Feed files the AI reasoning phase owns. Every other feed is written by the
# deterministic phase, which runs first and independently — so these two aging
# while the rest stay fresh is the fingerprint of a tick whose reasoning half
# is failing on every run. It is silent otherwise: the vault keeps its
# structure, the feeds keep arriving, and nothing anywhere says the brain
# stopped thinking. On one deployment it ran six days.
_AI_PHASE_FEED_IDS = frozenset({"operator-intent", "last-tick-diff"})

_SEVERITY_ORDER = {"ok": 0, "warn": 1, "fail": 2}


def _now(now: datetime | None = None) -> datetime:
    return now or datetime.now(tz=timezone.utc)


def _age_seconds(ts: float, now: datetime) -> float:
    return max(0.0, now.timestamp() - ts)


def _resolution_time(path: Path) -> float:
    """When a queue record reached its current directory.

    `mv` preserves mtime, so a request file carries its *enqueue* time forever;
    ctime is what moves when the drain files it. Taking the max reads correctly
    on both — and on filesystems where ctime is unavailable or clamped.

    Known blind spot: a whole-vault restore (`cp -a`, tar, rsync, a docker
    volume copy) stamps a fresh ctime on every record at once, so immediately
    after one, the relative order of completed/ and failed/ records is the
    restore tool's traversal order rather than true chronology, and this stage's
    verdict can flip either way for one tick cycle. `brain_keeper` uses the same
    recipe but only ever to *retain* a record, where a fresh ctime is the safe
    direction; here it decides which of two records came last, where it is not.
    No better signal exists in the record itself — the first real tick after a
    restore resolves it.
    """
    st = path.stat()
    return max(st.st_mtime, st.st_ctime)


def _newest(paths: list[Path]) -> tuple[Path | None, float]:
    best: Path | None = None
    best_ts = 0.0
    for p in paths:
        try:
            ts = p.stat().st_mtime
        except OSError:
            continue
        if ts > best_ts:
            best, best_ts = p, ts
    return best, best_ts


def _glob(root: Path, pattern: str, limit: int | None = None) -> list[Path]:
    """Bounded glob. Never returns more than `limit` (default MAX_SCAN) paths.

    Every count in this report is used for a threshold comparison, never for
    an exact inventory, so a truncated list gives the same verdict at a bounded
    cost. Unbounded is not an option: the vault is an NFS mount in production
    and this endpoint is callable by anyone holding the bearer token.
    """
    cap = MAX_SCAN if limit is None else limit
    out: list[Path] = []
    try:
        for p in root.glob(pattern):
            if not p.is_file():
                continue
            out.append(p)
            if len(out) >= cap:
                break
    except OSError:
        return out
    return out


def _walk(base: Path, keep: Callable[[str], Any], budget: int = MAX_SCAN) -> list[Path]:
    """Depth-first walk keeping files whose name satisfies `keep`.

    Bounded on *nodes visited*, not on matches. `Path.glob` with `**` bounds
    only the results, so a subtree holding no match still costs a full walk
    before it can say so — the exact shape that makes an endpoint expensive on
    an NFS vault. `_backups` is pruned: those are the reconcile's own safety
    copies, never live content.
    """
    found: list[Path] = []
    stack = [base]
    visited = 0
    while stack and visited < budget:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    visited += 1
                    if visited >= budget:
                        break
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name != "_backups":
                            stack.append(Path(entry.path))
                    elif keep(entry.name):
                        found.append(Path(entry.path))
        except OSError:
            continue
    return found


def _region_docs(root: Path, budget: int = MAX_SCAN) -> list[Path]:
    """Every markdown doc the arc embedder would index, wherever it now lives.

    Graduation writes an arc to whichever of the six region roots its own
    region maps to, and the inbox drain files them into nested subdirectories
    under those roots. A check that looks only at `left/*.md` therefore sees
    none of the arcs in `right/`, `bridge/`, `pineal/`, or any subdirectory —
    and reports a completely dead embedder as healthy, which is the one
    outcome this whole report exists to prevent.

    The set mirrors `embed_arcs.scan_arcs`: all of REGION_DIRS, recursively,
    minus lesson logs, which the lesson pass owns.
    """
    docs: list[Path] = []
    for region in REGION_DIRS:
        base = root / region
        if not base.is_dir():
            continue
        docs.extend(
            _walk(
                base,
                lambda n: n.endswith(".md") and not LESSON_LOG_RE.match(n),
                budget,
            )
        )
    return docs


def _authored(paths: list[Path]) -> list[Path]:
    """Drop the scaffold's own furniture from a set of content files.

    `README.md` documents what a directory is for; it is not something an agent
    wrote. Counting it inflates every "how much is in here" number and, worse,
    pins every "how old is the newest" number to the day the vault was created.
    """
    return [p for p in paths if p.name != "README.md" and not p.name.startswith(".")]


def _worst(*statuses: str) -> str:
    return max(statuses, key=lambda s: _SEVERITY_ORDER.get(s, 0))


def _hours(seconds: float) -> float:
    return round(seconds / 3600.0, 1)


# ---------------------------------------------------------------------------
# Stage 1 — ingest: are markers reaching the vault at all?
# ---------------------------------------------------------------------------


def check_ingest(root: Path, now: datetime) -> dict:
    """Newest write per marker destination.

    The four `write_marker` destinations are the only proof available on this
    side of the wire that agents are still emitting. The client-side outbox
    buffer is invisible here — `agentibrain check` inspects that locally.
    """
    # The scaffold seeds a README.md into amygdala/ and daily/. Counting those
    # as markers made a brand-new brain report "no marker written in 278h" —
    # measuring the age of the vault itself and calling a fresh install quiet.
    # Nothing has been written there at all, which is a different statement.
    destinations: dict[str, list[Path]] = {
        "lesson": _glob(root, "left/reference/lessons-*.md"),
        "signal": _authored(_glob(root, "amygdala/*.md")),
        "decision": _glob(root, "left/decisions/ADR-*.md"),
        "milestone": _authored(_glob(root, "daily/*.md"))
        + _glob(root, "left/projects/*/BLOCKS.md"),
    }

    detail: dict[str, Any] = {}
    newest_overall = 0.0
    total = 0
    for kind, paths in destinations.items():
        newest_path, newest_ts = _newest(paths)
        total += len(paths)
        newest_overall = max(newest_overall, newest_ts)
        detail[kind] = {
            "files": len(paths),
            "newest": newest_path.name if newest_path else None,
            "age_hours": _hours(_age_seconds(newest_ts, now)) if newest_ts else None,
        }

    if total == 0:
        return {
            "status": "ok",
            "note": "no markers written yet — nothing to verify",
            "destinations": detail,
        }

    quiet_hours = _hours(_age_seconds(newest_overall, now))
    if quiet_hours > MARKER_QUIET_HOURS:
        return {
            "status": "warn",
            "marker_files": total,
            "quiet_for_hours": quiet_hours,
            "destinations": detail,
            "hint": (
                f"no marker written in {quiet_hours}h. Agents may not be emitting, or the "
                "agentihooks writer is buffering — run `agentibrain sync` to replay the outbox."
            ),
        }
    return {
        "status": "ok",
        "marker_files": total,
        "newest_age_hours": quiet_hours,
        "destinations": detail,
    }


# ---------------------------------------------------------------------------
# Stage 2 — drain: is the tick queue being consumed, and do ticks succeed?
# ---------------------------------------------------------------------------


def check_drain(root: Path, now: datetime) -> dict:
    """The queue's own record of whether ticks run and whether they work.

    Two independent failures live here and must not be conflated: the drain not
    *picking up* work (requests pile up in requested/) and the tick *failing*
    once picked up (records land in failed/ carrying an error_tail). A stack can
    drain perfectly and still never complete a tick.
    """
    feed = root / BRAIN_FEED_DIR / "ticks"
    pending = _glob(feed, "requested/*.json")
    completed = _glob(feed, "completed/*.json")
    failed = _glob(feed, "failed/*.json")

    oldest_pending_age = 0.0
    oldest_pending_name = None
    for p in pending:
        try:
            requested_at = json.loads(p.read_text(encoding="utf-8")).get("requested_at", "")
            ts = datetime.fromisoformat(requested_at).timestamp()
        except (OSError, ValueError, json.JSONDecodeError, TypeError):
            try:
                ts = p.stat().st_mtime
            except OSError:
                continue
        age = _age_seconds(ts, now)
        if age > oldest_pending_age:
            oldest_pending_age, oldest_pending_name = age, p.name

    def _last(paths: list[Path]) -> tuple[Path | None, float]:
        best: Path | None = None
        best_ts = 0.0
        for p in paths:
            try:
                ts = _resolution_time(p)
            except OSError:
                continue
            if ts > best_ts:
                best, best_ts = p, ts
        return best, best_ts

    last_ok, last_ok_ts = _last(completed)
    last_bad, last_bad_ts = _last(failed)

    detail: dict[str, Any] = {
        "pending": len(pending),
        "completed": len(completed),
        "failed": len(failed),
        "last_completed_age_hours": _hours(_age_seconds(last_ok_ts, now)) if last_ok_ts else None,
        "last_failed_age_hours": _hours(_age_seconds(last_bad_ts, now)) if last_bad_ts else None,
    }

    # "When did a tick last RUN" is not "when was /tick last called". The cron
    # invokes brain_tick.py directly and leaves no queue record at all, so a
    # queue-only measure reports a busy stack as stale. What every tick does
    # leave behind is a regenerated feed file, so the newest of those is the
    # honest clock — and the queue's own last success still counts, for a
    # deployment driven entirely on demand.
    #
    # Computed here rather than after the verdicts, deliberately: it used to sit
    # below the failure branch, so the one number that distinguishes "failing
    # right now" from "failed once, has ticked fine since" was missing from the
    # report exactly when the operator needed it to tell those apart.
    _, newest_feed_ts = _newest(_glob(root / BRAIN_FEED_DIR, "*.md"))
    last_activity = max(last_ok_ts, newest_feed_ts)
    activity_hours = _hours(_age_seconds(last_activity, now)) if last_activity else None
    detail["last_tick_activity_hours"] = activity_hours

    if not pending and not completed and not failed:
        return {"status": "ok", "note": "no ticks requested yet — nothing to verify", **detail}

    # A request older than the stuck threshold means nothing is consuming the
    # queue. This is the single most useful signal in the whole report: every
    # downstream stage is fed by the tick, so a wedged drain explains all of them.
    if oldest_pending_age > PENDING_STUCK_SECONDS:
        return {
            "status": "fail",
            **detail,
            "oldest_pending": oldest_pending_name,
            "oldest_pending_age_minutes": round(oldest_pending_age / 60, 1),
            "hint": (
                "tick-drain is not consuming the queue. Check it is running and current: "
                "`agentibrain status`, `agentibrain logs tick-drain --since 10m`. "
                "An image older than the code needs `agentibrain build`."
            ),
        }

    # The most recent *resolution* decides the verdict: an old pile of failures
    # with a fresh success is a recovered stack, not a broken one.
    if last_bad_ts and last_bad_ts >= last_ok_ts:
        error_head = ""
        if last_bad is not None:
            try:
                tail = json.loads(last_bad.read_text(encoding="utf-8")).get("error_tail", "")
                error_head = " ".join(str(tail).split())[-400:]
            except (OSError, ValueError, json.JSONDecodeError):
                error_head = ""
        # A queue record is only written for a tick someone REQUESTED. The cron
        # leaves none, so an old failure stays the newest record indefinitely
        # while the stack ticks along fine — and the stage stayed red forever
        # with no way back to green and no hint that the way back is to request
        # a tick. When something has demonstrably ticked since the failure, the
        # honest verdict is "I cannot tell from here", not "broken".
        ticked_since = last_activity > last_bad_ts
        return {
            "status": "warn" if ticked_since else "fail",
            **detail,
            "last_resolution": "failed",
            "last_failed_record": last_bad.name if last_bad else None,
            "error_tail": error_head or None,
            "hint": (
                (
                    f"the newest queue record is a failure from {detail['last_failed_age_hours']}h "
                    f"ago, but something ticked {activity_hours}h ago — scheduled ticks leave no "
                    "queue record, so this stage cannot see them. Run `agentibrain tick --wait` "
                    "for a current verdict. The error_tail above is why that older one failed."
                )
                if ticked_since
                else (
                    "the most recent tick failed — every downstream stage is running on "
                    "whatever the last successful tick left behind. The error_tail above is "
                    "the reason; `agentibrain logs tick-drain --since 1h` has the full trace."
                )
            ),
        }

    if last_activity and activity_hours > TICK_STALE_HOURS:
        return {
            "status": "warn",
            **detail,
            "last_resolution": "completed" if last_ok_ts else None,
            "hint": (
                f"nothing has ticked in {activity_hours}h "
                f"(threshold {TICK_STALE_HOURS}h) — no completed request and no regenerated "
                "feed file. The tick cron is not firing; every stage below is as old as this."
            ),
        }
    return {"status": "ok", **detail, "last_resolution": "completed" if last_ok_ts else None}


# ---------------------------------------------------------------------------
# Stage 3 — arcs: is session work being clustered and ranked?
# ---------------------------------------------------------------------------


def check_arcs(root: Path, now: datetime) -> dict:
    arcs = _glob(root, f"{CLUSTERS_DIR}/*/*.md")
    graduated = _region_docs(root)
    newest_arc, newest_ts = _newest(arcs)

    hot = root / BRAIN_FEED_DIR / "hot-arcs.md"
    hot_rows = 0
    hot_age = None
    if hot.is_file():
        try:
            text = hot.read_text(encoding="utf-8")
            hot_rows = sum(1 for ln in text.splitlines() if ln.startswith("| 20"))
            hot_age = _hours(_age_seconds(hot.stat().st_mtime, now))
        except OSError:
            pass

    detail = {
        "clustered_arcs": len(arcs),
        "graduated_docs": len(graduated),
        "newest_arc": newest_arc.name if newest_arc else None,
        "newest_arc_age_hours": _hours(_age_seconds(newest_ts, now)) if newest_ts else None,
        "hot_arcs_rows": hot_rows,
        "hot_arcs_age_hours": hot_age,
    }

    if not arcs and not graduated:
        return {"status": "ok", "note": "no arcs yet — nothing to verify", **detail}
    # Keyed on *clustered* arcs, deliberately. Region docs include the standing
    # documents the scaffold ships (bridge/vision.md and its siblings), which a
    # brand-new vault has before anything has ever been clustered — demanding a
    # hot-arcs feed on their account would fail every fresh install.
    if arcs and not hot.is_file():
        return {
            "status": "fail",
            **detail,
            "hint": (
                "arcs exist but brain-feed/hot-arcs.md was never written — the tick's "
                "ranking phase has not run. Check the drain stage above first."
            ),
        }
    if arcs and hot_rows == 0:
        return {
            "status": "warn",
            **detail,
            "hint": (
                "hot-arcs.md holds no arc rows. Either every arc has cooled below the "
                "promote threshold, or ranking is filtering everything out."
            ),
        }
    return {"status": "ok", **detail}


# ---------------------------------------------------------------------------
# Stage 4 — lessons: written, kept in one place, and fed back
# ---------------------------------------------------------------------------


def _lesson_entries(path: Path) -> list[datetime]:
    """Parsed timestamps of the entries in one lesson log."""
    stamps: list[datetime] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return stamps
    for block in _LESSON_ENTRY_SPLIT_RE.split(text):
        head = block.lstrip().splitlines()[:1]
        if not head:
            continue
        m = _LESSON_ENTRY_HEADER_RE.match(head[0].strip())
        if not m:
            continue
        try:
            dt = datetime.fromisoformat(m.group(1))
        except ValueError:
            continue
        stamps.append(dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc))
    return stamps


def _scattered_lessons(root: Path) -> list[str]:
    """Lesson logs sitting anywhere but left/reference/.

    Their presence means the reconcile phase is not running: a lesson log
    outside the canonical directory is invisible to the feed writer and to the
    lesson embedder, which is exactly how months of lessons became unreachable.
    """
    canonical_dir = root / "left" / "reference"
    found: list[str] = []
    for region in (*REGION_DIRS, CLUSTERS_DIR):
        base = root / region
        if not base.is_dir():
            continue
        for p in _walk(base, LESSON_LOG_RE.match):
            if p.parent == canonical_dir:
                continue
            try:
                found.append(str(p.relative_to(root)))
            except ValueError:
                continue
    return sorted(found)


def check_lessons(root: Path, now: datetime) -> dict:
    canonical = _glob(root, "left/reference/lessons-*.md")
    scattered = _scattered_lessons(root)

    stamps: list[datetime] = []
    for p in canonical:
        stamps.extend(_lesson_entries(p))
    newest_entry = max(stamps) if stamps else None

    feed_path = root / BRAIN_FEED_DIR / "lessons.md"
    feed_entries = 0
    feed_age = None
    if feed_path.is_file():
        try:
            body = feed_path.read_text(encoding="utf-8")
            feed_entries = sum(1 for ln in body.splitlines() if ln.startswith("- "))
            feed_age = _hours(_age_seconds(feed_path.stat().st_mtime, now))
        except OSError:
            pass

    detail: dict[str, Any] = {
        "logs": len(canonical),
        "entries": len(stamps),
        "newest_entry": newest_entry.isoformat(timespec="seconds") if newest_entry else None,
        "scattered_logs": len(scattered),
        "feed_entries": feed_entries,
        "feed_age_hours": feed_age,
    }
    if scattered:
        detail["scattered_paths"] = scattered[:10]

    if not canonical and not scattered:
        return {"status": "ok", "note": "no lessons written yet — nothing to verify", **detail}

    if scattered:
        return {
            "status": "fail",
            **detail,
            "hint": (
                f"{len(scattered)} lesson log(s) outside left/reference/ — the tick's reconcile "
                "phase is not healing them, so their content reaches neither the feed nor the "
                "index. A current image reconciles on the next tick."
            ),
        }
    if not feed_path.is_file():
        return {
            "status": "fail",
            **detail,
            "hint": (
                "lessons exist but brain-feed/lessons.md was never written — nothing "
                "injects them into a session. Check the drain stage above."
            ),
        }

    # A capped feed that is empty while recent lessons exist means the window
    # filter or the writer is wrong — the failure the whole lesson pipeline
    # was built to close.
    recent = [s for s in stamps if (now - s).days <= LESSON_WINDOW_DAYS]
    if recent and feed_entries == 0:
        return {
            "status": "fail",
            **detail,
            "recent_in_window": len(recent),
            "hint": (
                f"{len(recent)} lesson(s) inside the {LESSON_WINDOW_DAYS}-day window but the "
                "feed is empty — the feed writer is dropping them."
            ),
        }
    if feed_entries > LESSON_FEED_MAX:
        return {
            "status": "warn",
            **detail,
            "hint": f"feed holds {feed_entries} entries, above the cap of {LESSON_FEED_MAX}.",
        }
    return {"status": "ok", **detail, "recent_in_window": len(recent)}


# ---------------------------------------------------------------------------
# Stage 5 — signals: alerts fire, and expire
# ---------------------------------------------------------------------------


def check_signals(root: Path, now: datetime) -> dict:
    """Signal files, the broadcast feed, and whether the feed is current.

    Whether an individual broadcast has outlived its TTL is decided by the
    tick's sweep, and re-deriving that here would fork the rule. What this
    stage establishes instead is the precondition for trusting the sweep at
    all: that the feed was regenerated recently. A signals.md older than the
    tick cadence is stale by definition, and everything it broadcasts —
    including a nuclear alert that should already have expired — is being
    read from a file nothing has revisited.
    """
    # README.md is the directory's own documentation — the scaffold seeds it,
    # and counting it inflated the file count and pinned `oldest` to the day
    # the vault was created rather than to the oldest live alarm.
    signal_files = _authored(_glob(root, "amygdala/*.md"))
    _, newest_ts = _newest(signal_files)
    oldest_ts = (
        min((p.stat().st_mtime for p in signal_files if p.is_file()), default=0.0)
        if signal_files
        else 0.0
    )

    feed_path = root / BRAIN_FEED_DIR / "signals.md"
    by_severity: dict[str, int] = {}
    entries = 0
    feed_age = None
    if feed_path.is_file():
        try:
            for ln in feed_path.read_text(encoding="utf-8").splitlines():
                if not ln.startswith("- **["):
                    continue
                entries += 1
                sev = ln.split("[", 1)[1].split("]", 1)[0].strip().lower()
                by_severity[sev] = by_severity.get(sev, 0) + 1
            feed_age = _hours(_age_seconds(feed_path.stat().st_mtime, now))
        except (OSError, IndexError):
            pass

    detail: dict[str, Any] = {
        "signal_files": len(signal_files),
        "newest_signal_age_hours": _hours(_age_seconds(newest_ts, now)) if newest_ts else None,
        "oldest_signal_age_days": round(_age_seconds(oldest_ts, now) / 86400, 1)
        if oldest_ts
        else None,
        "broadcast_entries": entries,
        "broadcast_by_severity": by_severity,
        "feed_age_hours": feed_age,
    }

    if not signal_files and not feed_path.is_file():
        return {"status": "ok", "note": "no signals raised yet — nothing to verify", **detail}
    if signal_files and not feed_path.is_file():
        return {
            "status": "fail",
            **detail,
            "hint": (
                "signal files exist but brain-feed/signals.md was never written — no alert "
                "reaches a session. Check the drain stage above."
            ),
        }
    if feed_age is not None and feed_age > FEED_STALE_HOURS:
        return {
            "status": "warn",
            **detail,
            "hint": (
                f"signals.md has not been rewritten in {feed_age}h — its TTL sweep has not run, "
                "so expired alerts are still being broadcast. Fix the tick, not the file."
            ),
        }
    return {"status": "ok", **detail}


# ---------------------------------------------------------------------------
# Stage 6 — feed: does anything actually reach a session?
# ---------------------------------------------------------------------------


def check_feed(root: Path, now: datetime) -> dict:
    """What `GET /feed` would serve right now, and how old each part of it is.

    This is the stage a session sees. Everything upstream can be healthy and
    this still be empty, because a feed file needs frontmatter and a non-empty
    body to be read at all — a writer that emits a header and no rows produces
    silence indistinguishable from calm.
    """
    try:
        entries = read_feed(root)
    except OSError as exc:
        return {"status": "fail", "error": str(exc)[:300], "hint": "brain-feed/ is unreadable."}

    feed_dir = root / BRAIN_FEED_DIR
    files = _glob(feed_dir, "*.md")
    by_id: dict[str, Path] = {}
    for f in files:
        try:
            head = f.read_text(encoding="utf-8")[:400]
        except OSError:
            continue
        for line in head.splitlines():
            if line.startswith("id:"):
                by_id.setdefault(line.split(":", 1)[1].strip(), f)
                break

    ages: dict[str, float] = {}
    newest = 0.0
    for e in entries:
        source = by_id.get(e.id) or (feed_dir / f"{e.id}.md")
        try:
            ts = source.stat().st_mtime
        except OSError:
            continue
        ages[e.id] = _hours(_age_seconds(ts, now))
        newest = max(newest, ts)

    detail: dict[str, Any] = {
        "served_entries": len(entries),
        "feed_files": len(files),
        "ids": [e.id for e in entries],
        "age_hours": ages,
    }

    if not files:
        return {"status": "ok", "note": "brain-feed/ is empty — nothing to verify", **detail}
    if not entries:
        return {
            "status": "fail",
            **detail,
            "hint": (
                "brain-feed holds files but none parse as feed entries — a feed file needs "
                "YAML frontmatter and a non-empty body. Nothing is reaching sessions."
            ),
        }

    # Deliberately NOT compared against each entry's own `ttl`. That field is a
    # consumer cache hint — how long agentihooks may reuse an injected block —
    # not a promise about how often the file is rewritten. Several feeds are
    # written only by the AI phase or only when their content changes, so
    # age-vs-ttl flags a healthy stack. What is unambiguous is *nothing* having
    # been regenerated: the whole directory going cold means no tick has run.
    freshest = _hours(_age_seconds(newest, now)) if newest else None
    detail["freshest_age_hours"] = freshest

    # The reasoning phase failing while the deterministic phase succeeds.
    lagging = {
        fid: age
        for fid, age in ages.items()
        if fid in _AI_PHASE_FEED_IDS
        and freshest is not None
        and age - freshest > AI_PHASE_LAG_HOURS
    }
    if lagging:
        detail["ai_phase_lag_hours"] = lagging
        return {
            "status": "warn",
            **detail,
            "hint": (
                "the deterministic half of the tick is running but the reasoning half is "
                f"not: {', '.join(f'{k} is {v}h old' for k, v in sorted(lagging.items()))} "
                f"against a freshest feed of {freshest}h. Only the AI phase writes those. "
                "Check the drain stage's error_tail, or run `agentibrain tick --wait`; a "
                "model too slow for BRAIN_LLM_TIMEOUT_SECONDS produces exactly this shape."
            ),
        }

    if freshest is not None and freshest > FEED_STALE_HOURS:
        return {
            "status": "warn",
            **detail,
            "hint": (
                f"no feed file has been rewritten in {freshest}h — every session is being "
                "injected with context that old. The tick that regenerates them is behind."
            ),
        }
    return {"status": "ok", **detail}


# ---------------------------------------------------------------------------
# Stage 7 — index: is any of it retrievable?
# ---------------------------------------------------------------------------


def check_index(root: Path, index_stats: dict | None) -> dict:
    """Cross-check embeddings row counts against the sources that feed them.

    The failure this catches is silent by construction: search returns fewer
    results rather than an error, so a producer with zero rows looks exactly
    like a query with no matches.
    """
    if index_stats is None:
        return {
            "status": "warn",
            "note": "embeddings /stats unreachable — index coverage unverified",
        }
    if index_stats.get("error"):
        return {
            "status": "warn",
            "error": str(index_stats["error"])[:300],
            "hint": "embeddings service did not answer /stats; older images do not expose it.",
        }

    raw = index_stats.get("producers")
    if not isinstance(raw, list):
        return {
            "status": "warn",
            "error": f"embeddings /stats returned no producer list ({type(raw).__name__})",
            "hint": "embeddings service answered but not in the expected shape.",
        }
    producers = {p.get("producer"): p for p in raw if isinstance(p, dict)}

    # `brain-arc` covers every region doc the embedder walks, not just
    # clusters/ — an arc graduated into right/ or bridge/ is still its input,
    # and a check that missed those called a dead embedder healthy.
    has_source = {
        "brain-arc": bool(
            _glob(root, f"{CLUSTERS_DIR}/*/*.md", limit=1)
            or _region_docs(root, budget=MAX_SCAN)[:1]
        ),
        "brain-lesson": bool(_glob(root, "left/reference/lessons-*.md", limit=1)),
        # Node-budgeted rather than `raw/**/*.md`: a subtree with no markdown
        # in it still costs a full walk under a recursive glob before it can
        # report the absence.
        "brain-raw": bool(_walk(root / "raw", lambda n: n.endswith(".md"))[:1])
        if (root / "raw").is_dir()
        else False,
    }

    detail: dict[str, Any] = {
        "total_rows": index_stats.get("total_rows"),
        "producers": {
            name: {"rows": p.get("rows"), "keys": p.get("keys")} for name, p in producers.items()
        },
        "source_present": {name: present for name, present in has_source.items() if present},
    }

    # A missing key, a null, or a zero all mean the same thing: this producer
    # indexes nothing. Only an explicitly positive count clears the check.
    def _keys(name: str) -> int:
        try:
            return int(producers.get(name, {}).get("keys") or 0)
        except (TypeError, ValueError):
            return 0

    # A wholly empty index on a vault that has never ticked is a cold start,
    # not a fault; on one that has, it is the fault. A written feed file is the
    # evidence that a tick ran, so it is what separates the two.
    if not any(_keys(name) for name in has_source) and any(has_source.values()):
        if not _glob(root / BRAIN_FEED_DIR, "*.md", limit=1):
            return {"status": "ok", "note": "index not built yet — no tick has run", **detail}
        return {
            "status": "fail",
            **detail,
            "missing_producers": sorted(n for n, v in has_source.items() if v),
            "hint": (
                "a tick has run but the index holds no rows at all — the embedder pass is "
                "not running, so nothing in this vault can be found by semantic search."
            ),
        }

    missing = [name for name, present in has_source.items() if present and _keys(name) == 0]
    if missing:
        return {
            "status": "fail",
            **detail,
            "missing_producers": missing,
            "hint": (
                f"source files exist for {', '.join(missing)} but the index holds no rows — "
                "the embedder pass for that producer is not running, so this content cannot "
                "be found by search at all."
            ),
        }
    return {"status": "ok", **detail}


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

_STAGES: tuple[tuple[str, Callable[..., dict]], ...] = (
    ("ingest", check_ingest),
    ("drain", check_drain),
    ("arcs", check_arcs),
    ("lessons", check_lessons),
    ("signals", check_signals),
    ("feed", check_feed),
)


def pipeline_report(
    vault_root: Path | None = None,
    now: datetime | None = None,
    index_stats: dict | None = None,
) -> dict:
    """Run every stage and fold the verdicts into one report.

    A stage that raises is reported as its own failure rather than taking the
    whole report down: a partial report still tells the operator where to look,
    and a 500 tells them nothing.
    """
    root = Path(vault_root) if vault_root else VAULT_ROOT
    ts = _now(now)

    stages: dict[str, Any] = {}
    for name, fn in _STAGES:
        try:
            stages[name] = fn(root, ts)
        except Exception as exc:  # noqa: BLE001 — one bad stage must not sink the report
            stages[name] = {"status": "fail", "error": f"{type(exc).__name__}: {exc}"[:300]}
    try:
        stages["index"] = check_index(root, index_stats)
    except Exception as exc:  # noqa: BLE001
        stages["index"] = {"status": "fail", "error": f"{type(exc).__name__}: {exc}"[:300]}

    overall = "ok"
    for stage in stages.values():
        overall = _worst(overall, stage.get("status", "ok"))

    return {
        "status": {"ok": "ok", "warn": "degraded", "fail": "broken"}[overall],
        "service": "brain-api",
        "vault_root": str(root),
        "checked_at": ts.isoformat(timespec="seconds"),
        "stages": stages,
    }
