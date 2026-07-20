#!/usr/bin/env python3
"""Credential redaction for anything the brain persists or injects.

Why this exists as its own module: on 2026-07-20 a single GitHub installation
token, leaked once into one session's `@signal` marker, was found in **72
places across 66 vault files**. The brain had amplified it — the signal was fed
into every tick prompt, and every tick persisted the model's echo of it to
`brain-feed/ticks/<ts>-ai-output.md`. A credential that entered the vault once
was re-copied every two hours for days, then re-injected into agent context via
the signals feed.

The lesson is that any store which both ingests agent output and replays it
needs redaction at the boundary, not just at the source. Apply `scrub()` to
anything written to the vault or emitted to an agent.

`extract_workflow.py` carries its own older SECRET_RE for step parameters; this
module is the superset (it adds the GitHub `ghs_`/`gho_`/`github_pat_` forms
that missed the 2026-07-20 token, plus GitLab, Slack and generic assignments).
"""

from __future__ import annotations

import re

PLACEHOLDER = "<REDACTED_SECRET>"

SECRET_RE = re.compile(
    "|".join(
        (
            # GitHub — ghp_ (classic PAT), ghs_ (server/installation),
            # gho_ (OAuth), ghu_ (user-to-server), github_pat_ (fine-grained).
            # ghs_ is the form that went undetected for three months.
            r"gh[psou]_[A-Za-z0-9]{20,}",
            r"github_pat_[A-Za-z0-9_]{20,}",
            # OpenAI / Anthropic
            r"sk-ant-[A-Za-z0-9_\-]{20,}",
            r"sk-[A-Za-z0-9]{20,}",
            # Slack
            r"xox[baprs]-[A-Za-z0-9\-]{10,}",
            # AWS
            r"AKIA[0-9A-Z]{16}",
            # Google
            r"AIza[0-9A-Za-z_\-]{20,}",
            # GitLab
            r"glpat-[A-Za-z0-9_\-]{15,}",
            # Bearer headers
            r"Bearer\s+[A-Za-z0-9._\-]{20,}",
        )
    )
)

# `TOKEN=abc…` / `api_key: "abc…"` — catches shapes the prefix patterns miss.
ASSIGNMENT_RE = re.compile(
    r"""(?ix)
    \b(
        [A-Z0-9_]*
        (?: SECRET | PASSWORD | PASSWD | TOKEN | API[_-]?KEY | ACCESS[_-]?KEY | PRIVATE[_-]?KEY )
        [A-Z0-9_]*
    )
    \s* [:=] \s*
    ["'`]?
    (?P<value> [A-Za-z0-9/+_\-\.]{16,} )
    ["'`]?
    """
)


def scrub(text: str) -> str:
    """Replace credential-looking substrings with a placeholder.

    Conservative on the assignment form: only the value is replaced, so the
    surrounding prose still reads (`TOKEN=<REDACTED_SECRET>`), keeping the
    incident legible without keeping the secret.
    """
    if not text:
        return text
    out = SECRET_RE.sub(PLACEHOLDER, text)
    out = ASSIGNMENT_RE.sub(
        lambda m: m.group(0).replace(m.group("value"), PLACEHOLDER), out
    )
    return out


def contains_secret(text: str) -> bool:
    """True when `text` still looks like it carries a credential."""
    if not text:
        return False
    return bool(SECRET_RE.search(text) or ASSIGNMENT_RE.search(text))
