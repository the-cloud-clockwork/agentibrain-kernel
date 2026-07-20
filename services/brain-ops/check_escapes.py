#!/usr/bin/env python3
"""Fail on invalid string escape sequences in this package.

Why a dedicated check rather than trusting `python -W error::SyntaxWarning`:

  1. Bytecode caching. Once __pycache__ exists the module is not recompiled,
     so the warning never fires again. A local "clean" run can be measured
     against stale .pyc files while the container — which always compiles
     fresh — warns on every start.
  2. Version skew. The dev workbench runs 3.10; the image is 3.12, which
     reports more of these. A check that passes locally can still be noisy in
     production.

This walks the token stream instead, so it is deterministic, cache-independent
and version-independent. Raw literals are skipped — a backslash there is
intentional.

Usage:  python3 check_escapes.py [dir]     (exit 1 if any found)
"""

from __future__ import annotations

import re
import sys
import tokenize
from pathlib import Path

# Escapes Python recognises inside a non-raw string literal.
VALID_ESCAPES = set("ntr\\'\"abfv01234567xNuU\n")
_BACKSLASH = re.compile(r"\\(.)", re.DOTALL)


def _prefix_of(literal: str) -> str:
    """Return the lowercased prefix (r/b/u/f...) of a string literal."""
    stripped = literal.lstrip("rRbBuUfF")
    return literal[: len(literal) - len(stripped)].lower()


def check(root: Path) -> list[str]:
    problems: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            with open(path, "rb") as fh:
                tokens = list(tokenize.tokenize(fh.readline))
        except (tokenize.TokenError, SyntaxError, OSError) as e:
            problems.append(f"{path}: unreadable ({e})")
            continue
        for tok in tokens:
            if tok.type != tokenize.STRING or "r" in _prefix_of(tok.string):
                continue
            for m in _BACKSLASH.finditer(tok.string):
                if m.group(1) not in VALID_ESCAPES:
                    problems.append(
                        f"{path}:{tok.start[0]}: invalid escape sequence "
                        f"'\\{m.group(1)}' — use a raw string (r\"...\") or double the backslash"
                    )
    return problems


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).parent)
    problems = check(root)
    for p in problems:
        print(p, file=sys.stderr)
    if problems:
        print(f"\n{len(problems)} invalid escape sequence(s)", file=sys.stderr)
        return 1
    print(f"no invalid escape sequences under {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
