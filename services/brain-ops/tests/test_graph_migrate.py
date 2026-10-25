"""SV2-GRF-02: evidence-aware edge normalization and the graph migrator.

T-SV2-GRF-02-A, B and C over the package fixture: duplicate markers,
contradictory directions, a valid multirelation pair and a self loop.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_BRAIN_OPS = _HERE.parent
for _p in (_BRAIN_OPS, _BRAIN_OPS / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import brain_apply  # noqa: E402
import edge_relations  # noqa: E402
import graph_migrate  # noqa: E402

FIXTURE = _HERE / "fixtures" / "sv2_grf02_edges.json"
OUTPUTS = ("relations.json", "quarantine.json", "audit.json", "manifest.json")

EXPECTED_RELATIONS = {
    ("arc-a", "parent", "arc-p"): ["left/arc-a.md#L13", "left/arc-a.md#L14", "left/arc-p.md#L11"],
    ("arc-a", "related", "arc-p"): ["left/arc-a.md#L15", "left/arc-p.md#L12"],
    ("arc-a", "supersedes", "arc-old"): ["left/arc-a.md#L18"],
}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _vault(root: Path) -> Path:
    for rel, text in json.loads(FIXTURE.read_text())["files"].items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


def _load(out: Path, name: str):
    return json.loads((out / name).read_text())


def _relations(out: Path) -> dict[tuple[str, str, str], dict]:
    return {(r["source"], r["type"], r["target"]): r for r in _load(out, "relations.json")}


def _entry(out: Path, reason: str) -> dict:
    return next(q for q in _load(out, "quarantine.json") if reason in q["reasons"])


def _commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=_HERE, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _result(case: str, vault: Path, manifest: dict, **extra) -> None:
    record = {
        "package": "SV2-GRF-02",
        "case": case,
        "tested_commit": _commit(),
        "inputs": {rel: _sha((vault / rel).read_bytes()) for rel in manifest["inputs"]},
        "graph_migration_quarantined_edges": manifest["graph_migration_quarantined_edges"],
        **extra,
    }
    print("GRF02_RESULT " + json.dumps(record, sort_keys=True))


class TestNormalize:
    def test_inverse_child_is_stored_as_parent(self):
        a = edge_relations.Assertion("arc-p", "child", "arc-a", "p#L1", "inline")
        b = edge_relations.Assertion("arc-a", "parent", "arc-p", "a#L1", "inline")
        out = edge_relations.normalize([a, b])
        assert [(r["source"], r["type"], r["target"]) for r in out["relations"]] == [
            ("arc-a", "parent", "arc-p")
        ]
        assert out["relations"][0]["evidence"] == ["p#L1", "a#L1"]
        assert [d["decision"] for d in out["audit"]] == ["accepted", "merged_duplicate"]

    def test_symmetric_relation_merges_both_directions(self):
        out = edge_relations.normalize(
            [
                edge_relations.Assertion("arc-b", "related", "arc-a", "b#L1", "inline"),
                edge_relations.Assertion("arc-a", "related", "arc-b", "a#L1", "inline"),
            ]
        )
        assert [(r["source"], r["type"], r["target"]) for r in out["relations"]] == [
            ("arc-a", "related", "arc-b")
        ]

    def test_contradiction_never_picks_a_ranked_winner(self):
        out = edge_relations.normalize(
            [
                edge_relations.Assertion("arc-m", "parent", "arc-n", "m#L1", "inline"),
                edge_relations.Assertion("arc-m", "sibling", "arc-n", "m#L2", "inline"),
            ]
        )
        assert out["relations"] == []
        assert out["quarantine"][0]["reasons"] == ["conflicting_types"]
        assert {tuple(r["relation"]) for r in out["quarantine"][0]["relations"]} == {
            ("arc-m", "parent", "arc-n"),
            ("arc-m", "sibling", "arc-n"),
        }


class TestPositive:
    """T-SV2-GRF-02-A: valid parent, child and related links survive with their evidence."""

    def test_valid_links_survive_with_meaning_and_evidence(self, tmp_path):
        vault = _vault(tmp_path / "vault")
        out = tmp_path / "graph"
        manifest = graph_migrate.migrate(vault, out)

        relations = _relations(out)
        assert {k: r["evidence"] for k, r in relations.items()} == EXPECTED_RELATIONS
        assert all(r["status"] == "accepted" for r in relations.values())
        assert all(r["confidence"] == "extracted" for r in relations.values())
        assert relations[("arc-a", "parent", "arc-p")]["scope"] == {
            "source": "alpha",
            "target": "alpha",
        }
        assert relations[("arc-a", "supersedes", "arc-old")]["scope"]["target"] == "unknown"

        audit = _load(out, "audit.json")
        assert len(audit) == 13
        decisions = [d["decision"] for d in audit]
        assert decisions.count("accepted") == 3
        assert decisions.count("merged_duplicate") == 3
        assert decisions.count("quarantined") == 7
        assert manifest["graph_migration_quarantined_edges"] == 7
        assert manifest["relations"] == 3
        for name in ("relations.json", "quarantine.json", "audit.json"):
            assert manifest["outputs"][name] == _sha((out / name).read_bytes())
        for rel, digest in manifest["inputs"].items():
            assert digest == _sha((vault / rel).read_bytes())

        second_vault = _vault(tmp_path / "second" / "vault")
        second_out = tmp_path / "second" / "graph"
        graph_migrate.migrate(second_vault, second_out)
        for name in OUTPUTS:
            assert (second_out / name).read_bytes() == (out / name).read_bytes()
        _result("A", vault, manifest, relations=sorted("|".join(k) for k in relations))

    def test_cli_reports_the_quarantine_measurement(self, tmp_path, capsys):
        vault = _vault(tmp_path / "vault")
        assert graph_migrate.main(["--vault", str(vault), "--out", str(tmp_path / "graph")]) == 0
        assert "graph_migration_quarantined_edges=7" in capsys.readouterr().out

    def test_tick_writes_each_relation_of_a_multirelation_pair(self, tmp_path):
        vault = _vault(tmp_path / "vault")
        applied = brain_apply.apply_edges(
            vault,
            [
                {"source": "arc-x", "type": "supersedes", "target": "arc-old", "reason": "r"},
                {"source": "arc-x", "type": "related", "target": "arc-old", "reason": "r"},
                {"source": "arc-a", "type": "parent", "target": "arc-p", "reason": "r"},
            ],
            dry_run=False,
        )
        assert applied == 2
        text = (vault / "right" / "arc-x.md").read_text()
        assert "<!-- @edge type=supersedes target=arc-old -->" in text
        assert "<!-- @edge type=related target=arc-old -->" in text
        assert (vault / "left" / "arc-a.md").read_text().count("type=parent") == 2


class TestRejection:
    """T-SV2-GRF-02-B: conflicting assertions are quarantined, never resolved by rank."""

    def test_conflicts_are_quarantined_and_the_vault_is_untouched(self, tmp_path):
        vault = _vault(tmp_path / "vault")
        before = _snapshot(vault)
        out = tmp_path / "graph"
        manifest = graph_migrate.migrate(vault, out)
        assert _snapshot(vault) == before

        relations = _relations(out)
        for pair in (("arc-x", "arc-y"), ("arc-a", "arc-s")):
            assert not [k for k in relations if {k[0], k[2]} == set(pair)]

        contradiction = _entry(out, "contradictory_direction")
        assert {tuple(r["relation"]) for r in contradiction["relations"]} == {
            ("arc-x", "parent", "arc-y"),
            ("arc-y", "parent", "arc-x"),
        }
        conflict = _entry(out, "conflicting_types")
        assert {tuple(r["relation"]) for r in conflict["relations"]} == {
            ("arc-a", "sibling", "arc-s"),
            ("arc-s", "parent", "arc-a"),
        }
        assert conflict["relations"][0]["evidence"] or conflict["relations"][1]["evidence"]
        assert _entry(out, "self_loop")["relations"][0]["relation"] == ["arc-a", "related", "arc-a"]
        assert _entry(out, "unsupported_type")["relations"][0]["relation"] == [
            "arc-a",
            "causes",
            "arc-p",
        ]
        assert _entry(out, "malformed")["relations"][0]["evidence"] == [
            "clusters/2026-10-01/arc-old.md#L7"
        ]
        assert all(q["review"] is None and q["present"] for q in _load(out, "quarantine.json"))

        for name in OUTPUTS:
            assert "private body text" not in (out / name).read_text()
        _result(
            "B",
            vault,
            manifest,
            quarantine=sorted(r for q in _load(out, "quarantine.json") for r in q["reasons"]),
        )

    def test_tick_refuses_a_relation_that_contradicts_the_vault(self, tmp_path):
        vault = _vault(tmp_path / "vault")
        before = _snapshot(vault)
        applied = brain_apply.apply_edges(
            vault,
            [
                {"source": "arc-p", "type": "parent", "target": "arc-a", "reason": "r"},
                {"source": "arc-x", "type": "sibling", "target": "arc-old", "reason": "r"},
                {"source": "arc-x", "type": "parent", "target": "arc-old", "reason": "r"},
                {"source": "arc-x", "type": "causes", "target": "arc-old", "reason": "r"},
            ],
            dry_run=False,
        )
        assert applied == 0
        assert _snapshot(vault) == before


class TestRecovery:
    """T-SV2-GRF-02-C: reruns are idempotent and reviewed decisions stay intact."""

    def _review(self, out: Path, qid: str, relation: list[str]) -> bytes:
        body = json.dumps(
            {qid: {"decision": "accept", "relation": relation, "reviewer": "operator"}},
            sort_keys=True,
        ).encode()
        (out / "reviews.json").write_bytes(body)
        return body

    def test_rerun_keeps_reviews_and_rollback_restores_the_previous_projection(
        self, tmp_path, monkeypatch
    ):
        vault = _vault(tmp_path / "vault")
        out = tmp_path / "graph"
        graph_migrate.migrate(vault, out)
        qid = _entry(out, "contradictory_direction")["id"]
        reviews = self._review(out, qid, ["arc-x", "parent", "arc-y"])

        graph_migrate.migrate(vault, out)
        reviewed = _relations(out)[("arc-x", "parent", "arc-y")]
        assert reviewed["evidence"] == ["right/arc-x.md#L7", f"review:{qid}"]
        assert _entry(out, "contradictory_direction")["review"]["decision"] == "accept"
        stable = {name: (out / name).read_bytes() for name in OUTPUTS}

        graph_migrate.migrate(vault, out)
        assert {name: (out / name).read_bytes() for name in OUTPUTS} == stable
        assert (out / "reviews.json").read_bytes() == reviews

        (vault / "right" / "arc-y.md").write_text("---\ncluster_id: arc-y\n---\n\n# Arc Y\n")
        real_write = graph_migrate._atomic_write

        def crash_on_manifest(path: Path, data: bytes) -> None:
            if path.name == "manifest.json":
                raise OSError("interrupted")
            real_write(path, data)

        monkeypatch.setattr(graph_migrate, "_atomic_write", crash_on_manifest)
        with pytest.raises(OSError):
            graph_migrate.migrate(vault, out)
        monkeypatch.setattr(graph_migrate, "_atomic_write", real_write)

        manifest = graph_migrate.migrate(vault, out)
        clean = tmp_path / "clean"
        clean_vault = _vault(clean / "vault")
        (clean_vault / "right" / "arc-y.md").write_text("---\ncluster_id: arc-y\n---\n\n# Arc Y\n")
        (clean / "graph").mkdir()
        (clean / "graph" / "reviews.json").write_bytes(reviews)
        graph_migrate.migrate(clean_vault, clean / "graph")
        for name in OUTPUTS:
            assert (out / name).read_bytes() == (clean / "graph" / name).read_bytes()

        kept = _entry(out, "contradictory_direction")
        assert kept["present"] is False and kept["review"]["relation"] == [
            "arc-x",
            "parent",
            "arc-y",
        ]
        assert (out / "reviews.json").read_bytes() == reviews
        _result("C", vault, manifest, reviewed=qid)

        vault_before = _snapshot(vault)
        assert graph_migrate.main(["--out", str(out), "--rollback"]) == 0
        for name in OUTPUTS:
            assert (out / name).read_bytes() == stable[name]
        assert _snapshot(vault) == vault_before

    def test_rollback_refuses_a_previous_projection_that_fails_its_manifest(self, tmp_path):
        vault = _vault(tmp_path / "vault")
        out = tmp_path / "graph"
        graph_migrate.migrate(vault, out)
        (vault / "right" / "arc-x.md").write_text("---\ncluster_id: arc-x\n---\n\n# Arc X\n")
        graph_migrate.migrate(vault, out)
        current = {name: (out / name).read_bytes() for name in OUTPUTS}

        (out / "previous" / "relations.json").write_text("[]\n")
        with pytest.raises(graph_migrate.RollbackRefused):
            graph_migrate.rollback(out)
        assert {name: (out / name).read_bytes() for name in OUTPUTS} == current

    def test_rollback_without_a_previous_projection_is_refused(self, tmp_path):
        assert graph_migrate.main(["--out", str(tmp_path / "graph"), "--rollback"]) == 1
        assert not os.path.exists(tmp_path / "graph" / "relations.json")
