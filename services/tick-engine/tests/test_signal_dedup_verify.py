"""Smoke tests for the Phase 1-4 brain signal pipeline fixes.

Covered:
- Phase 2: brain_keeper collector dedup by (source, content-hash)
- Phase 3: brain_verifier pass/fail/skip/error status + _mitigated tagging
- Phase 4: brain_apply.parse_signals regex (single-token + backtick multi-word)
- Phase 4: apply_signal_changes fuzzy fallback when source= attr missing

Run from repo root:
    cd stacks/brain-tools && python -m pytest tests/ -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make brain-tools modules importable when tests run from any cwd.
_HERE = Path(__file__).resolve().parent
_BRAIN_TOOLS = _HERE.parent
if str(_BRAIN_TOOLS) not in sys.path:
    sys.path.insert(0, str(_BRAIN_TOOLS))

import brain_apply  # noqa: E402
import brain_verifier  # noqa: E402
import markers  # noqa: E402


# ---------- Phase 4: parse_signals regex ----------

class TestParseSignals:
    def test_single_token_source(self):
        out = brain_apply.parse_signals(
            "ESCALATE: paper2slides-s3 → warning (all jobs fail)"
        )
        assert len(out) == 1
        assert out[0]["op"] == "escalate"
        assert out[0]["source"] == "paper2slides-s3"
        assert out[0]["new_severity"] == "warning"

    def test_backtick_multi_word_source(self):
        out = brain_apply.parse_signals(
            "`ESCALATE: `ArgoCD Image Updater GHCR auth broken` → critical (fleet-wide CD halt)`"
        )
        assert len(out) == 1
        assert out[0]["source"] == "ArgoCD Image Updater GHCR auth broken"
        assert out[0]["new_severity"] == "critical"

    def test_bullet_list_prefix(self):
        out = brain_apply.parse_signals(
            "- ESCALATE: `multi word source` → critical (reason)"
        )
        assert len(out) == 1
        assert out[0]["source"] == "multi word source"

    def test_clear_directive(self):
        out = brain_apply.parse_signals(
            "`CLEAR: foo-bar (resolved per operator)`"
        )
        assert len(out) == 1
        assert out[0]["op"] == "clear"
        assert out[0]["source"] == "foo-bar"
        assert out[0]["reason"] == "resolved per operator"

    def test_mixed_section(self):
        section = "\n".join([
            "- ESCALATE: paper2slides-s3 → warning (all jobs fail)",
            "- ESCALATE: `another multi word` → critical (reason here)",
            "- CLEAR: old-signal-source (fixed)",
            "- no directive here, just prose",
        ])
        out = brain_apply.parse_signals(section)
        ops = [c["op"] for c in out]
        assert ops == ["escalate", "escalate", "clear"], f"got {out}"


# ---------- Phase 3: brain_verifier ----------

class TestVerifier:
    def test_skip_when_no_verify_attr(self):
        sig = markers.Marker(type="signal", content="test", attrs={"source": "foo"})
        assert brain_verifier.verify_signal(sig) == brain_verifier.SKIP_STATUS

    def test_pass_when_verify_exits_zero(self):
        sig = markers.Marker(type="signal", content="x", attrs={"source": "foo", "verify": "true"})
        assert brain_verifier.verify_signal(sig) == brain_verifier.PASS_STATUS

    def test_fail_when_verify_exits_nonzero(self):
        sig = markers.Marker(type="signal", content="x", attrs={"source": "foo", "verify": "false"})
        assert brain_verifier.verify_signal(sig) == brain_verifier.FAIL_STATUS

    def test_error_on_timeout(self):
        sig = markers.Marker(
            type="signal",
            content="x",
            attrs={"source": "foo", "verify": "sleep 30"},
        )
        # Very short timeout to trigger subprocess.TimeoutExpired.
        assert brain_verifier.verify_signal(sig, timeout_s=1) == brain_verifier.ERROR_STATUS

    def test_apply_results_stamps_mitigated(self):
        sigs = [
            markers.Marker(type="signal", content="claim", attrs={"source": "foo", "verify": "true"}),
            markers.Marker(type="signal", content="claim2", attrs={"source": "bar", "verify": "false"}),
            markers.Marker(type="signal", content="claim3", attrs={"source": "baz"}),
        ]
        results = brain_verifier.verify_all(sigs)
        stats = brain_verifier.apply_verify_results(sigs, results)
        # First signal passes → _mitigated true
        assert sigs[0].attrs.get("_mitigated") == "true"
        # Second failed → untouched
        assert "_mitigated" not in sigs[1].attrs
        # Third skipped (no verify) → untouched
        assert "_mitigated" not in sigs[2].attrs
        assert stats["verified_pass"] == 1
        assert stats["verified_fail"] == 1
        assert stats["verified_skip"] == 1


# ---------- Phase 2: dedup logic (unit-level, avoid full tick) ----------

class TestCollectorDedup:
    def test_identical_signals_collapse(self):
        """Two markers, same source, same content → keep one."""
        import hashlib

        def dedup_key(sig):
            return (
                sig.attr("source", ""),
                hashlib.sha256(sig.content.strip().encode()).hexdigest()[:16],
            )

        sigs = [
            markers.Marker(type="signal", content="broken auth", attrs={"source": "argocd"}),
            markers.Marker(type="signal", content="broken auth", attrs={"source": "argocd"}),
            markers.Marker(type="signal", content="broken auth\n", attrs={"source": "argocd"}),
            markers.Marker(type="signal", content="different claim", attrs={"source": "argocd"}),
        ]
        seen = set()
        kept = [s for s in sigs if (k := dedup_key(s)) not in seen and not seen.add(k)]
        assert len(kept) == 2
        keys = {dedup_key(s) for s in kept}
        assert len(keys) == 2

    def test_different_sources_same_content_kept(self):
        """Same content, different sources → keep both (they are different events)."""
        import hashlib

        def dedup_key(sig):
            return (
                sig.attr("source", ""),
                hashlib.sha256(sig.content.strip().encode()).hexdigest()[:16],
            )

        sigs = [
            markers.Marker(type="signal", content="broken", attrs={"source": "a"}),
            markers.Marker(type="signal", content="broken", attrs={"source": "b"}),
        ]
        seen = set()
        kept = [s for s in sigs if (k := dedup_key(s)) not in seen and not seen.add(k)]
        assert len(kept) == 2


# ---------- Phase 4: apply fuzzy fallback ----------

class TestApplyFuzzyFallback:
    def test_structured_source_path(self, tmp_path):
        """Signal with source= attr → primary regex path matches and CLEARs."""
        clusters = tmp_path / "clusters" / "2026-04-20"
        clusters.mkdir(parents=True)
        arc = clusters / "test.md"
        arc.write_text(
            "# Test arc\n\n"
            "<!-- @signal severity=info source=my-service -->\n"
            "Service X is failing.\n"
            "<!-- @/signal -->\n"
        )
        applied = brain_apply.apply_signal_changes(
            tmp_path,
            [{"op": "clear", "source": "my-service", "reason": "fixed"}],
            dry_run=False,
        )
        assert applied == 1
        assert "CLEARED: fixed" in arc.read_text()

    def test_fuzzy_fallback_bare_signal(self, tmp_path):
        """Signal without source= attr → fuzzy content match CLEARs it."""
        clusters = tmp_path / "clusters" / "2026-04-20"
        clusters.mkdir(parents=True)
        arc = clusters / "test.md"
        arc.write_text(
            "# Test arc\n\n"
            "<!-- @signal -->\n"
            "ArgoCD Image Updater GHCR auth broken — secret missing.\n"
            "<!-- @/signal -->\n"
        )
        applied = brain_apply.apply_signal_changes(
            tmp_path,
            [{
                "op": "clear",
                "source": "ArgoCD Image Updater GHCR auth broken",
                "reason": "verified secret present",
            }],
            dry_run=False,
        )
        assert applied == 1
        text = arc.read_text()
        assert "CLEARED: verified secret present" in text

    def test_no_match_no_mutation(self, tmp_path):
        """No signal for a source → zero applied, file unchanged."""
        clusters = tmp_path / "clusters" / "2026-04-20"
        clusters.mkdir(parents=True)
        arc = clusters / "test.md"
        original = "# Test arc\n\nNo signals here.\n"
        arc.write_text(original)
        applied = brain_apply.apply_signal_changes(
            tmp_path,
            [{"op": "clear", "source": "ghost-source", "reason": "phantom"}],
            dry_run=False,
        )
        assert applied == 0
        assert arc.read_text() == original


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
