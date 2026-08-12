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
# numbers, ticket ids.
_TOKEN_RE = re.compile(r"\b[0-9a-f]{7,40}\b", re.IGNORECASE)
_SHA_PREFIX_LEN = 7


def incident_keys(text: str) -> set[str]:
    """Identifiers in `text` specific enough to name one incident.

    Two token shapes, keyed differently, because they have different structure:

    **All-digit tokens are matched whole.** A CI run id is a near-sequential
    global counter, so two runs from the same day routinely share their leading
    digits and differ only at the end. Truncating them to a prefix does not
    identify an incident — it identifies a *time window of about ten thousand
    runs*. Run 31518579981 ("checkout is down") and run 31518571119 ("stale
    cache key") both begin 3151857, and a prefix rule let the resolved cache bug
    silently close the live checkout outage. Nothing about a decimal counter
    justifies a prefix; it gets exact match.

    **Tokens containing a-f are treated as commit SHAs and keyed on their first
    seven characters.** Truncation is the point there: git itself abbreviates,
    slugs truncate, and humans quote the short form, so `6f8c941e` and the full
    forty-character hash have to reach the same key or an alarm never matches
    its own fix. Seven hex characters is 268 million values — a real identifier,
    unlike seven digits of a sequential counter.

    A token must contain a digit either way. Without that rule ordinary words
    spelled from the letters a-f — `defaced`, `feedbac` — become incident keys.

    A short SHA that happens to be all digits (about one in thirty) is treated
    as a counter and will not match its long form. That fails to *close* an
    alarm, never falsely closes one, and the age sweep still retires it.
    """
    keys: set[str] = set()
    for match in _TOKEN_RE.finditer(text or ""):
        token = match.group(0).lower()
        if not any(c.isdigit() for c in token):
            continue
        if token.isdigit():
            keys.add(token)
        else:
            keys.add(token[:_SHA_PREFIX_LEN])
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
        fm = arc.frontmatter
        status = str(fm.get("status", "")).strip().lower()
        severity = str(fm.get("severity", "")).strip().lower()

        # An explicit `resolves:` field is the precise instrument and always
        # wins: the author is naming exactly what they closed, so there is
        # nothing to infer.
        declared = fm.get("resolves")
        if declared:
            if isinstance(declared, str):
                keys |= incident_keys(declared)
            elif isinstance(declared, list):
                for item in declared:
                    keys |= incident_keys(str(item))

        # Two shapes, because a resolution arrives by two routes. The synthesis
        # phase writes an incident arc and sets `status`; an agent emitting a
        # `resolved` marker gets a file in amygdala/ whose *frontmatter* carries
        # the severity and whose body is plain prose — no `@signal` comment, so
        # the marker scan never sees it. Reading only one route left the write
        # path unable to close anything, which is the whole point of it.
        if status != "resolved" and severity != "resolved":
            continue

        if severity == "resolved":
            # A resolved-severity file is single-purpose: the whole document IS
            # the assertion, so every identifier in it is part of the claim.
            keys |= incident_keys(arc.body or "")

        # An arc's body is a narrative, and a good postmortem names every run it
        # investigated — including the ones it explicitly ruled out. Scraping it
        # closed those too. Only the identity fields speak for the arc itself.
        keys |= incident_keys(
            " ".join(str(fm.get(f, "")) for f in ("title", "summary", "cluster_id", "id"))
        )

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
