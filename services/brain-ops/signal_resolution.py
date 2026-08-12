"""Closing an alarm by evidence instead of by timer.

The staleness sweep in `brain_keeper.write_signals_feed` retires a signal when
its carrier ages past a window — five days for nuclear and critical. That is
the right fallback for a claim nobody can confirm or deny, and the wrong
instrument for one whose answer is already written down.

The case this module exists for: a deploy failure fired `nuclear` at 17:43,
and the fix landed at 17:44 with a note in the vault naming the same run id and
the same commit. Both documents sat in the vault together. The alarm kept
broadcasting, because nothing connected them — they carry different ids, and
the resolution was prose rather than a signal.

So connect them on what they actually share: the **incident key**. A run id, a
commit SHA, a ticket number — an identifier specific enough that two documents
naming it are talking about the same event. Extraction is deterministic and
needs no model call, which is what makes the rule testable and its failures
inspectable.

Closing is an explicit act, never inferred from age or status alone. Only two
things assert a resolution:

* a `@signal` marker whose severity is ``resolved``
* an arc whose frontmatter ``status`` is ``resolved``

`graduated` deliberately does not count. Graduation means an arc cooled and was
filed away; an alarm nobody acted on graduates exactly like one that was fixed,
so treating it as resolution would silently close live alarms — the failure
mode the timer already covers, with none of the timer's honesty.
"""

from __future__ import annotations

import re

# Hex-shaped tokens of seven characters or more: run ids, commit SHAs, build
# numbers, ticket ids. Digits are a subset of hex, so one pattern covers both a
# decimal run id and a hex SHA, and the seven-character normalization below
# makes a full SHA match its own short form — which matters, because slugs
# truncate and humans quote the short one.
_TOKEN_RE = re.compile(r"\b[0-9a-f]{7,40}\b", re.IGNORECASE)
_KEY_LEN = 7


def incident_keys(text: str) -> set[str]:
    """Identifiers in `text` specific enough to name one incident.

    A token must contain a digit. Without that rule ordinary words spelled from
    the letters a-f — `defaced`, `feedbac` — become incident keys, and a single
    unlucky word in a resolution note would close an unrelated alarm.
    """
    keys: set[str] = set()
    for match in _TOKEN_RE.finditer(text or ""):
        token = match.group(0).lower()
        if not any(c.isdigit() for c in token):
            continue
        keys.add(token[:_KEY_LEN])
    return keys


def resolved_keys(arcs, signals) -> set[str]:
    """Incident keys that something in the vault asserts are resolved.

    `arcs` is any iterable of parsed documents exposing `.frontmatter` and
    `.body`; `signals` any iterable of markers exposing `.attr` and `.content`.
    Both are passed in rather than re-read so this stays a pure function over
    the tick's existing scan.
    """
    keys: set[str] = set()

    for sig in signals or ():
        if sig.attr("severity", "").strip().lower() != "resolved":
            continue
        keys |= incident_keys(sig.content or "")

    for arc in arcs or ():
        status = str(arc.frontmatter.get("status", "")).strip().lower()
        severity = str(arc.frontmatter.get("severity", "")).strip().lower()
        # Two shapes, because a resolution arrives by two routes. The synthesis
        # phase writes an incident arc and sets `status`; an agent emitting a
        # `resolved` marker gets a file in amygdala/ whose *frontmatter* carries
        # the severity and whose body is plain prose — no `@signal` comment, so
        # the marker scan never sees it. Reading only one route left the write
        # path unable to close anything, which is the whole point of it.
        if status != "resolved" and severity != "resolved":
            continue
        # Title and summary carry the identifier as often as the body does —
        # a synthesized incident arc puts the run id in its summary line.
        haystack = " ".join(
            str(arc.frontmatter.get(field, ""))
            for field in ("title", "summary", "cluster_id", "id")
        )
        keys |= incident_keys(haystack)
        keys |= incident_keys(arc.body or "")

    return keys


def is_resolved(content: str, closed: set[str]) -> bool:
    """True when this signal names an incident something has closed.

    Requires a literal shared identifier. A signal carrying no identifier at
    all — a bare "disk filling up" — can never be closed this way, and falls
    through to the age sweep, which is correct: there is nothing to match on.
    """
    if not closed:
        return False
    return bool(incident_keys(content) & closed)
