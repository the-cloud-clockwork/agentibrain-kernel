"""POST /ingest — GitHub page links are page references; only bare repositories are cloned."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO = "https://github.com/the-cloud-clockwork/agentihooks"


@pytest.fixture()
def ingest(vault: Path, client, monkeypatch: pytest.MonkeyPatch):
    from app import router, vault_reader

    monkeypatch.setattr(router, "INFERENCE_URL", "")
    monkeypatch.setattr(vault_reader, "VAULT_ROOT", vault.resolve())
    clones: list[str] = []

    def fake_run(cmd, **kwargs):
        clones.append(cmd[-2])
        if cmd[-2] != REPO:
            raise subprocess.CalledProcessError(128, cmd, stderr=b"repository not found")
        Path(cmd[-1]).mkdir(parents=True)
        (Path(cmd[-1]) / "README.md").write_text("readme")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(router.subprocess, "run", fake_run)

    def post(message: str) -> tuple[dict, list[str]]:
        resp = client.post("/ingest", data={"message": message})
        assert resp.status_code == 200
        return resp.json(), clones

    return post


@pytest.mark.parametrize(
    "link",
    [
        f"{REPO}/pull/685",
        f"{REPO}/issues/607",
        f"{REPO}/issues/607#issuecomment-6005035902",
    ],
)
def test_page_link_is_a_page_reference_not_a_clone(vault: Path, ingest, link):
    data, clones = ingest(f"Merged the phase lifecycle change, proof at {link} today")

    assert clones == []
    assert data["errors"] == []
    assert data["page_refs"] == [{"url": link, "repository": REPO}]
    note = (vault / data["obsidian_path"]).read_text()
    assert link in note
    assert REPO in note


def test_bare_repository_link_is_still_cloned(ingest):
    data, clones = ingest(f"Look at {REPO} for the hooks")

    assert clones == [REPO]
    assert data["errors"] == []
    assert data["page_refs"] == []
    assert len(data["vault_paths"]) == 1
