"""Signals must expire, including the loud ones.

Nuclear and critical severities skipped the age sweep entirely, so a signal
broadcast forever unless an agent performed one of two exact rituals. In
practice that never happened: a CI run cancelled on 2026-08-05 was still firing
six days later, and forty vault files had accumulated complaining about it —
the brain raising alarm about its own inability to clear alarm.

Run from repo root:
    pytest -q services/brain-ops/tests
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_BRAIN_TOOLS = _HERE.parent
if str(_BRAIN_TOOLS) not in sys.path:
    sys.path.insert(0, str(_BRAIN_TOOLS))

import brain_keeper  # noqa: E402
import markers  # noqa: E402

NOW = datetime.now(timezone.utc)


def _sig(severity: str, age_days: float, **attrs) -> markers.Marker:
    created = (NOW - timedelta(days=age_days)).isoformat()
    return markers.Marker(
        type="signal",
        attrs={
            "severity": severity,
            "source": "github-actions",
            "_parent_arc_created": created,
            **attrs,
        },
        content=f"a {severity} signal aged {age_days}d",
    )


def test_nuclear_signal_expires_eventually(tmp_path: Path):
    """The reported symptom: a cancelled CI run still shouting six days on."""
    out = tmp_path / "signals.md"
    old = _sig("nuclear", brain_keeper.BRAIN_STALE_CRITICAL_DAYS + 1)

    stats = brain_keeper.write_signals_feed(out, [old], now=NOW)

    assert stats["tombstoned_stale"] == 1
    assert stats["written"] == 0
    assert "No active signals." not in out.read_text() or stats["written"] == 0


def test_fresh_nuclear_signal_still_broadcasts(tmp_path: Path):
    """A real credential event must not be swept the moment it is raised."""
    out = tmp_path / "signals.md"
    fresh = _sig("nuclear", 1)

    stats = brain_keeper.write_signals_feed(out, [fresh], now=NOW)

    assert stats["written"] == 1
    assert stats["tombstoned_stale"] == 0
    assert "nuclear" in out.read_text()


def test_nuclear_outlives_a_warning(tmp_path: Path):
    """Protected severities get a longer window, not the same one."""
    age = brain_keeper.BRAIN_STALE_SIGNAL_DAYS + 1
    assert age < brain_keeper.BRAIN_STALE_CRITICAL_DAYS
    out = tmp_path / "signals.md"

    stats = brain_keeper.write_signals_feed(
        out, [_sig("nuclear", age), _sig("warning", age)], now=NOW
    )

    assert stats["written"] == 1
    assert stats["tombstoned_stale"] == 1
    assert "nuclear" in out.read_text()


def test_ttl_days_attr_overrides_the_window(tmp_path: Path):
    """Same explicit knob @inject blocks already honour."""
    out = tmp_path / "signals.md"
    long_lived = _sig("warning", brain_keeper.BRAIN_STALE_SIGNAL_DAYS + 2, ttl_days="90")

    stats = brain_keeper.write_signals_feed(out, [long_lived], now=NOW)

    assert stats["written"] == 1
    assert stats["tombstoned_stale"] == 0


def test_reverified_signal_survives_the_age_sweep(tmp_path: Path):
    """A claim the verifier just re-confirmed is not an old record.

    The age sweep exists to retire claims nobody can confirm. A nuclear signal
    whose verify command re-asserts "still true" on every tick was swept anyway
    once its parent arc aged out — the exact false negative this whole
    mechanism must not produce.
    """
    import brain_verifier

    out = tmp_path / "signals.md"
    sig = _sig(
        "nuclear",
        brain_keeper.BRAIN_STALE_CRITICAL_DAYS + 5,
        verify="false",  # exits non-zero => FAIL => the claim stands
    )
    sig.content = "credential still exposed"

    results = brain_verifier.verify_all([sig])
    brain_verifier.apply_verify_results([sig], results)
    stats = brain_keeper.write_signals_feed(out, [sig], now=NOW)

    assert sig.attrs.get("_still_true") == "true"
    assert stats["written"] == 1
    assert stats["tombstoned_stale"] == 0
    assert "credential still exposed" in out.read_text()


def test_severity_matching_is_case_insensitive(tmp_path: Path):
    """A shift key must not cost a credential alert two days of life."""
    out = tmp_path / "signals.md"
    age = brain_keeper.BRAIN_STALE_SIGNAL_DAYS + 1  # inside critical, outside default
    variants = [_sig(s, age) for s in ("nuclear", "Nuclear", "NUCLEAR", " critical ")]

    stats = brain_keeper.write_signals_feed(out, variants, now=NOW)

    assert stats["written"] == 4
    assert stats["tombstoned_stale"] == 0


def test_unparseable_ttl_days_falls_back_and_warns(tmp_path: Path, capsys):
    """A typo'd override must not silently revert to the default."""
    out = tmp_path / "signals.md"
    sig = _sig("warning", 0.5, ttl_days="1.5")

    stats = brain_keeper.write_signals_feed(out, [sig], now=NOW)

    assert stats["written"] == 1
    assert "ttl_days" in capsys.readouterr().out
