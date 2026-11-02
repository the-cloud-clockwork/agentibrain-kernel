"""One writer for vault knowledge files: a shared lock, staged batches and a roll-forward journal.

The lock is the marker registry lock brain-api takes for every marker write, so a tick
apply and a marker burst never interleave. A batch keeps its writes in memory until
commit; commit stages each file, writes the journal, then replaces every target. A
journal left by a crash is replayed once, skipping any target changed since staging.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import shutil
from collections.abc import Iterator
from contextvars import ContextVar
from pathlib import Path
from uuid import uuid4

LOCK = Path("brain-feed") / "markers" / ".lock"
JOURNAL = Path("brain-feed") / "markers" / "knowledge-journal.json"
STAGING = Path("brain-feed") / "markers" / "knowledge-staging"

_BATCH: ContextVar[Batch | None] = ContextVar("knowledge_batch", default=None)


class Batch:
    """Writes and moves staged in memory for one vault, applied together by commit."""

    def __init__(self, vault: Path) -> None:
        """Start an empty batch for the vault."""
        self.vault = vault
        self.writes: dict[Path, str] = {}
        self.moves: list[tuple[Path, Path]] = []


def _digest(path: Path) -> str | None:
    """Hash of the file's bytes, or None when it does not exist."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


def _replace(path: Path, data: bytes) -> None:
    """Write data beside path and rename it over path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


@contextlib.contextmanager
def mutation_lock(vault: Path) -> Iterator[int]:
    """Hold the shared writer lock; yields 1 when another writer held it first, else 0."""
    path = vault / LOCK
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        conflicts = 0
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            conflicts = 1
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield conflicts
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_text(path: Path) -> str:
    """The file's text as the open batch would leave it."""
    batch = _BATCH.get()
    if batch is not None and path in batch.writes:
        return batch.writes[path]
    return path.read_text(encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    """Stage the text in the open batch, or replace the file atomically outside one."""
    batch = _BATCH.get()
    if batch is None:
        _replace(path, text.encode("utf-8"))
        return
    batch.writes[path] = text


def move(src: Path, dst: Path) -> None:
    """Stage a rename in the open batch, or rename now outside one."""
    batch = _BATCH.get()
    if batch is None:
        shutil.move(str(src), str(dst))
        return
    batch.moves.append((src, dst))


@contextlib.contextmanager
def staged(vault: Path) -> Iterator[Batch]:
    """Open a batch that collects writes and moves; nothing reaches disk until commit."""
    batch = Batch(vault)
    token = _BATCH.set(batch)
    try:
        yield batch
    finally:
        _BATCH.reset(token)


def _entries(batch: Batch, staging: Path) -> list[dict]:
    """Stage every write under staging and describe each operation for the journal."""
    entries: list[dict] = []
    root = batch.vault
    for n, (path, text) in enumerate(batch.writes.items()):
        data = text.encode("utf-8")
        staged_file = staging / f"{n}.stage"
        _replace(staged_file, data)
        entries.append(
            {
                "op": "write",
                "target": path.relative_to(root).as_posix(),
                "staged": staged_file.relative_to(root).as_posix(),
                "base": _digest(path),
                "new": hashlib.sha256(data).hexdigest(),
            }
        )
    for src, dst in batch.moves:
        content = batch.writes.get(src)
        base = (
            _digest(src) if content is None else hashlib.sha256(content.encode("utf-8")).hexdigest()
        )
        entries.append(
            {
                "op": "move",
                "src": src.relative_to(root).as_posix(),
                "dst": dst.relative_to(root).as_posix(),
                "base": base,
            }
        )
    return entries


def _write_journal(vault: Path, entries: list[dict]) -> None:
    """Persist the staged operations so a crash rolls forward."""
    _replace(vault / JOURNAL, json.dumps({"entries": entries}, sort_keys=True).encode("utf-8"))


def _apply_op(vault: Path, entry: dict) -> str:
    """Apply one journal entry: applied, done when already in place, conflict when changed."""
    if entry["op"] == "move":
        src, dst = vault / entry["src"], vault / entry["dst"]
        if not src.exists() and dst.exists():
            return "done"
        if _digest(src) != entry["base"] or dst.exists():
            return "conflict"
        shutil.move(str(src), str(dst))
        return "applied"
    target = vault / entry["target"]
    current = _digest(target)
    if current == entry["new"]:
        return "done"
    if current != entry["base"]:
        return "conflict"
    _replace(target, (vault / entry["staged"]).read_bytes())
    return "applied"


def _clear(vault: Path) -> int:
    """Remove the journal and every staged file; returns the staged files removed."""
    (vault / JOURNAL).unlink(missing_ok=True)
    staging = vault / STAGING
    if not staging.is_dir():
        return 0
    removed = 0
    for path in staging.iterdir():
        path.unlink()
        removed += 1
    return removed


def _run(vault: Path, entries: list[dict]) -> dict[str, int]:
    """Apply journal entries in order and clear the journal."""
    outcomes = [_apply_op(vault, entry) for entry in entries]
    _clear(vault)
    return {"replayed": outcomes.count("applied"), "conflicts": outcomes.count("conflict")}


def commit(batch: Batch) -> dict[str, int]:
    """Stage, journal and apply the batch; the caller holds mutation_lock."""
    if not batch.writes and not batch.moves:
        return {"applied": 0, "conflicts": 0}
    entries = _entries(batch, batch.vault / STAGING)
    _write_journal(batch.vault, entries)
    result = _run(batch.vault, entries)
    return {"applied": result["replayed"], "conflicts": result["conflicts"]}


def recover(vault: Path) -> dict[str, int]:
    """Roll an interrupted commit forward once, or discard staging left without a journal."""
    journal = vault / JOURNAL
    if journal.is_file():
        entries = json.loads(journal.read_text(encoding="utf-8"))["entries"]
        return {**_run(vault, entries), "discarded": 0}
    return {"replayed": 0, "conflicts": 0, "discarded": _clear(vault)}
