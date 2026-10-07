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

    def post(message: str, endpoint="/ingest", **fields) -> tuple[dict, list[str]]:
        resp = client.post(endpoint, data={"message": message, **fields})
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


@pytest.mark.parametrize("endpoint", ["/ingest", "/ingest_with_files"])
def test_bare_repository_in_prose_is_not_cloned(vault: Path, ingest, endpoint):
    data, clones = ingest(f"CI findings for {REPO}: tests passed", endpoint=endpoint)

    assert clones == []
    assert data["errors"] == []
    assert data["vault_paths"] == []
    assert REPO in (vault / data["obsidian_path"]).read_text()


@pytest.mark.parametrize("endpoint", ["/ingest", "/ingest_with_files"])
def test_explicit_repository_field_is_cloned(ingest, endpoint):
    data, clones = ingest("Read the requested repository", endpoint=endpoint, repository=REPO)

    assert clones == [REPO]
    assert data["errors"] == []
    assert len(data["vault_paths"]) == 1


@pytest.mark.parametrize("repository", ["", REPO])
def test_classifier_cannot_authorize_repository_clone(vault: Path, ingest, monkeypatch, repository):
    from unittest.mock import AsyncMock

    from app import router

    unrelated = "https://github.com/example/private-repository"
    monkeypatch.setattr(
        router,
        "_call_router_llm",
        AsyncMock(
            return_value={
                "semantic_text": "CI findings",
                "extractables": [{"type": "repo", "value": unrelated, "hint": "clone this"}],
            }
        ),
    )
    data, clones = ingest("CI findings naming an unrelated repository", repository=repository)

    assert clones == ([REPO] if repository else [])
    assert data["errors"] == []
    assert data["page_refs"] == [{"url": unrelated, "repository": unrelated}]
    assert unrelated in (vault / data["obsidian_path"]).read_text()
