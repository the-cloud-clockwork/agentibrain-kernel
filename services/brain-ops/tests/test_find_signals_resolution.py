"""A resolution must reach the resolver.

signal_resolution.py closes an alarm when something in the vault asserts the
same incident key is resolved. It can only act on markers find_signals hands
it — and `resolved` is not a rung on the severity ladder, so ranking it
numerically scored it 0, below the default `warning` threshold. The one
severity that retires a signal was the one severity the filter dropped.

Consequence in production: every `@signal severity=resolved` an agent wrote
was discarded here, while the alarm it answered kept broadcasting to the whole
fleet — the antoncore deploy failure of 2026-08-11 stayed nuclear for a day
with its own resolution sitting in the vault, unread.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import markers  # noqa: E402

_BODY = """
<!-- @signal severity=resolved source=deploy -->
antoncore dev deploy.failed (run 31518571119, SHA 6f8c941e) is resolved.
<!-- @/signal -->
<!-- @signal severity=info source=noise -->
routine
<!-- @/signal -->
<!-- @signal severity=nuclear source=fire -->
alarm
<!-- @/signal -->
"""


def _sevs(min_severity):
    return sorted(m.attr("severity") for m in markers.find_signals(_BODY, min_severity))


def test_resolved_survives_every_threshold():
    for threshold in ("info", "warning", "critical", "nuclear"):
        assert "resolved" in _sevs(threshold), (
            f"a resolution was dropped at min_severity={threshold}; "
            "signal_resolution can never close the alarm it answers"
        )


def test_ordinary_severities_still_filter():
    """The bypass is for resolutions only — it must not flatten the ladder."""
    assert _sevs("info") == ["info", "nuclear", "resolved"]
    assert "info" not in _sevs("warning")
    assert "nuclear" in _sevs("nuclear")


def test_the_resolution_carries_its_incident_keys():
    """Reaching the resolver is only half of it — it must still match."""
    from signal_resolution import incident_keys

    resolved = [m for m in markers.find_signals(_BODY) if m.attr("severity") == "resolved"]
    assert resolved, "fixture must contain a resolution"
    keys = incident_keys(resolved[0].content)
    assert "31518571119" in keys, "run id must key exactly"
    assert "6f8c941" in keys, "SHA must key on its 7-char prefix"
