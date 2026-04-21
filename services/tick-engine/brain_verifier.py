"""Signal auto-verifier for brain-keeper.

Each @signal marker may carry a `verify="<shell command>"` attribute. On every
tick, brain-keeper runs that command against the live fleet. If the command
exits 0, the signal's underlying claim is falsified and the signal is
auto-tombstoned (excluded from signals.md output) for the current tick.

This is the "live-re-verify" tombstone path that complements the existing
age-based and mitigation-arc paths in write_signals_feed.

Fail-safe: any exception, timeout, or unexpected status results in "keep the
signal". Silence cannot masquerade as success.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shlex
import subprocess

import markers

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 10
SKIP_STATUS = "skip"
PASS_STATUS = "pass"
FAIL_STATUS = "fail"
ERROR_STATUS = "error"


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.strip().encode("utf-8")).hexdigest()[:16]


def _signal_key(sig: markers.Marker) -> tuple[str, str]:
    return (sig.attr("source", ""), _content_hash(sig.content))


def verify_signal(sig: markers.Marker, timeout_s: int = DEFAULT_TIMEOUT_S) -> str:
    """Run the signal's verify command and return a status string.

    Returns one of: pass, fail, skip, error.
    - pass  = verify exited 0 → signal's claim is false → tombstone
    - fail  = verify exited non-zero → signal's claim stands → keep
    - skip  = no verify= attr → keep (caller decides)
    - error = exception / timeout / non-executable → keep + log
    """
    cmd = sig.attr("verify", "").strip()
    if not cmd:
        return SKIP_STATUS

    try:
        # Use shell=True because verify commands are intentionally free-form
        # pipes (e.g. `kubectl ... | jq -e '.data.credentials'`). The command
        # string lives in the operator's own vault and is run inside the
        # brain-keeper pod's sandboxed RBAC.
        result = subprocess.run(
            cmd,
            shell=True,
            timeout=timeout_s,
            capture_output=True,
            text=True,
            env={**os.environ, "LANG": "C"},
        )
        if result.returncode == 0:
            return PASS_STATUS
        log.debug("signal verify FAIL source=%s rc=%s stderr=%s",
                  sig.attr("source", "?"), result.returncode, result.stderr[:200])
        return FAIL_STATUS
    except subprocess.TimeoutExpired:
        log.warning("signal verify TIMEOUT source=%s cmd=%s", sig.attr("source", "?"), cmd[:80])
        return ERROR_STATUS
    except Exception as e:  # noqa: BLE001
        log.warning("signal verify ERROR source=%s exc=%s", sig.attr("source", "?"), e)
        return ERROR_STATUS


def verify_all(
    signals: list[markers.Marker],
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> dict[tuple[str, str], str]:
    """Verify every signal. Returns {(source, content_hash): status}.

    Fail-safe: a single verifier blowing up does not halt the tick. Each
    signal is verified independently.
    """
    results: dict[tuple[str, str], str] = {}
    if not _enabled():
        return results
    for sig in signals:
        key = _signal_key(sig)
        if key in results:
            continue  # dedup already handled upstream, defensive
        try:
            results[key] = verify_signal(sig, timeout_s=timeout_s)
        except Exception as e:  # noqa: BLE001
            log.warning("verify_all wrapper error key=%s exc=%s", key, e)
            results[key] = ERROR_STATUS
    return results


def apply_verify_results(
    signals: list[markers.Marker],
    results: dict[tuple[str, str], str],
) -> dict[str, int]:
    """Stamp `_mitigated=true` on signals whose verify command passed.

    The existing `write_signals_feed` tombstone path consumes `_mitigated`, so
    no changes are needed there. Returns stats dict for inclusion in the tick
    summary.
    """
    stats = {"verified_pass": 0, "verified_fail": 0, "verified_skip": 0, "verified_error": 0}
    for sig in signals:
        status = results.get(_signal_key(sig), SKIP_STATUS)
        stats[f"verified_{status}"] = stats.get(f"verified_{status}", 0) + 1
        if status == PASS_STATUS:
            sig.attrs["_mitigated"] = "true"
            sig.attrs["_mitigated_by"] = "verifier"
    return stats


def _enabled() -> bool:
    return os.environ.get("BRAIN_VERIFIER_ENABLED", "true").lower() not in {"0", "false", "no"}
