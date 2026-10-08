"""A completed Kubernetes tick certifies text and semantic retrieval.

Runs the brain-ops CronJob scripts exactly as the Helm chart renders them,
against real brain-api and embeddings processes on pgvector, from an empty
scaffolded vault. Only the upstream embedding model is a local stand-in.

Needs `helm` and TICK_CYCLE_POSTGRES_URL (a pgvector server whose user may
create databases); CI provides both.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
BRAIN_OPS = ROOT / "services" / "brain-ops"
CHART = ROOT / "helm" / "brain-ops"
POSTGRES_URL = os.environ.get("TICK_CYCLE_POSTGRES_URL", "")
BEARER = uuid.uuid4().hex
EMBED_BEARER = uuid.uuid4().hex
MODEL_BEARER = uuid.uuid4().hex
DIM = 256
DATABASE = "tick_cycle_e2e"

pytestmark = pytest.mark.skipif(
    not os.environ.get("CI") and (not POSTGRES_URL or shutil.which("helm") is None),
    reason="needs helm and TICK_CYCLE_POSTGRES_URL",
)


def _vector(text: str) -> list[float]:
    vec = [0.0] * DIM
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        vec[int(hashlib.sha256(word.encode()).hexdigest(), 16) % DIM] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


class _ModelHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        payload = json.dumps({"data": [{"embedding": _vector(body["input"])}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args) -> None:
        pass


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(cwd: Path, module: str, env: dict) -> tuple[subprocess.Popen, str]:
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", module, "--host", "127.0.0.1", "--port", str(port)],
        cwd=cwd,
        env={**os.environ, **env},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            if httpx.get(f"{url}/health", timeout=1).status_code == 200:
                return proc, url
        except httpx.HTTPError:
            time.sleep(0.2)
    proc.kill()
    raise RuntimeError(f"{module} did not start")


def _stop(proc: subprocess.Popen) -> None:
    proc.terminate()
    proc.wait(timeout=10)


def _database_url() -> str:
    import psycopg

    with psycopg.connect(POSTGRES_URL, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {DATABASE} WITH (FORCE)")
        conn.execute(f"CREATE DATABASE {DATABASE}")
    return POSTGRES_URL.rsplit("/", 1)[0] + f"/{DATABASE}"


@pytest.fixture(scope="module")
def embeddings():
    model = ThreadingHTTPServer(("127.0.0.1", 0), _ModelHandler)
    threading.Thread(target=model.serve_forever, daemon=True).start()
    db_url = _database_url()
    proc, url = _serve(
        ROOT / "services" / "embeddings" / "src",
        "main:app",
        {
            "POSTGRES_URL": db_url,
            "LLM_API_BASE": f"http://127.0.0.1:{model.server_port}",
            "LLM_API_KEY": MODEL_BEARER,
            "LLM_EMBED_MODEL": "stand-in",
            "EMBED_DIM": str(DIM),
            "AUTH_MODE": "required",
            "API_KEYS": EMBED_BEARER,
        },
    )
    yield {"url": url, "db": db_url}
    _stop(proc)
    model.shutdown()


@pytest.fixture()
def brain(tmp_path: Path, embeddings: dict):
    import psycopg

    from agentibrain.scaffold import scaffold

    with psycopg.connect(embeddings["db"], autocommit=True) as conn:
        conn.execute("TRUNCATE content_embeddings")
    vault = tmp_path / "vault"
    scaffold(vault)
    proc, url = _serve(
        ROOT / "services" / "brain-api",
        "app.main:app",
        {
            "VAULT_ROOT": str(vault),
            "KB_ROUTER_TOKENS": BEARER,
            "KB_ROUTER_TOKEN": BEARER,
            "INFERENCE_URL": "",
            "AMYGDALA_SIGNAL_PATH": "brain-feed/amygdala-active.md",
            "EMBEDDINGS_URL": "",
        },
    )
    yield {
        "vault": vault,
        "api": httpx.Client(base_url=url, headers={"Authorization": f"Bearer {BEARER}"}),
        "embeddings": embeddings,
    }
    _stop(proc)


def _job_script(template: str, values: list[str]) -> tuple[str, dict]:
    rendered = subprocess.run(
        ["helm", "template", "brain", str(CHART), "--show-only", template, *values],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    doc = yaml.safe_load(rendered)
    container = doc["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e["value"] for e in container.get("env", []) if "value" in e}
    return container["command"][-1], env


def _run_job(template: str, vault: Path, env: dict, values: list[str] | None = None):
    script, chart_env = _job_script(template, values or [])
    script = script.replace("/app/", f"{BRAIN_OPS}/").replace("/vault", str(vault))
    script = re.sub(r"\bpython3\b", sys.executable, script)
    script = script.replace("sleep 5", "true")
    base = {k: v for k, v in os.environ.items() if not k.startswith(("EMBED", "INFERENCE"))}
    return subprocess.run(
        ["bash", "-c", script],
        env={**base, **chart_env, "REDIS_URL": "", "CLICKHOUSE_URL": "", **env},
        capture_output=True,
        text=True,
        timeout=300,
    )


def _drain(brain: dict, embeddings_url: str | None = None, extra: dict | None = None):
    env = {
        "EMBEDDINGS_URL": embeddings_url or brain["embeddings"]["url"],
        "EMBEDDINGS_API_KEY": EMBED_BEARER,
        **(extra or {}),
    }
    return _run_job("templates/tick-drain-cronjob.yaml", brain["vault"], env)


def _ingest(brain: dict, token: str) -> None:
    api = brain["api"]
    note = api.post(
        "/ingest",
        data={"message": f"Plain text field note: the {token} harbour crane lifts containers."},
    )
    assert note.status_code == 200, note.text
    assert note.json()["obsidian_path"].startswith("raw/inbox/")
    marker = api.post(
        "/marker",
        json={
            "type": "lesson",
            "content": f"Lesson {token}: rebuild the ballast pump before every voyage.",
            "attrs": {"source": "tick-cycle-e2e"},
        },
    )
    assert marker.status_code == 201, marker.text


def _request_tick(brain: dict, **params) -> str:
    resp = brain["api"].post("/tick", params={"source": "tick-cycle-e2e", **params})
    assert resp.status_code == 202, resp.text
    return resp.json()["job_id"]


def _status(brain: dict, job_id: str) -> dict:
    return brain["api"].get(f"/tick/{job_id}").json()


def _text_hits(brain: dict, token: str) -> list[str]:
    resp = brain["api"].get("/vault/search", params={"q": token})
    return [r["path"] for r in resp.json()["results"]]


def _semantic_hits(brain: dict, query: str) -> list[dict]:
    resp = httpx.post(
        f"{brain['embeddings']['url']}/search",
        json={"query": query, "limit": 50},
        headers={"Authorization": f"Bearer {EMBED_BEARER}"},
    )
    assert resp.status_code == 200, resp.text
    return [r for r in resp.json()["results"] if query in (r["text_preview"] or "")]


def _rows(brain: dict, token: str) -> list[tuple]:
    import psycopg

    with psycopg.connect(brain["embeddings"]["db"]) as conn:
        return conn.execute(
            "SELECT key, chunk_idx, producer FROM content_embeddings WHERE text_preview LIKE %s",
            (f"%{token}%",),
        ).fetchall()


def _lesson_entries(vault: Path, token: str) -> int:
    logs = (vault / "left" / "reference").glob("lessons-*.md")
    return sum(log.read_text(encoding="utf-8").count(f"Lesson {token}:") for log in logs)


def _vault_digest(vault: Path) -> dict[str, str]:
    queue = vault / "brain-feed" / "ticks"
    return {
        str(p.relative_to(vault)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(vault.rglob("*"))
        if p.is_file() and queue not in p.parents
    }


def _token() -> str:
    return "zx" + uuid.uuid4().hex[:10]


def test_completed_tick_serves_text_and_semantic_reads(brain: dict) -> None:
    token = _token()
    raw = brain["vault"] / "raw" / "notes" / f"{token}-staged.md"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(f"Staged raw note {token}rawstage about tide tables and harbour pilots.\n")
    _ingest(brain, token)
    job = _request_tick(brain)
    assert _status(brain, job)["status"] == "pending"
    assert _semantic_hits(brain, token) == []

    result = _drain(brain)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _status(brain, job)["status"] == "completed"
    text = _text_hits(brain, token)
    assert any(p.startswith("left/reference/lessons-") for p in text), text
    assert any(not p.startswith("raw/") and "lessons-" not in p for p in text), text
    producers = {hit["producer"] for hit in _semantic_hits(brain, token)}
    assert {"brain-arc", "brain-lesson"} <= producers, producers
    raw_hits = _semantic_hits(brain, f"{token}rawstage")
    assert [h["producer"] for h in raw_hits] == ["brain-raw"], raw_hits


def test_index_failure_is_never_completed_and_retry_resumes_once(brain: dict) -> None:
    token = _token()
    _ingest(brain, token)
    job = _request_tick(brain)

    planted = _drain(brain, embeddings_url=f"http://127.0.0.1:{_free_port()}")

    status = _status(brain, job)
    assert status["status"] != "completed", planted.stdout + planted.stderr
    assert status["status"] == "pending"
    assert status["index_attempts"] == 1
    assert status["last_error"]
    assert _semantic_hits(brain, token) == []

    retried = _drain(brain)

    assert retried.returncode == 0, retried.stdout + retried.stderr
    assert "maintenance: resumed" in retried.stdout
    assert _status(brain, job)["status"] == "completed"
    assert _lesson_entries(brain["vault"], token) == 1
    rows = _rows(brain, token)
    assert len(rows) == len(set(rows)), rows
    assert sorted(r[2] for r in rows) == ["brain-arc", "brain-lesson"], rows


def test_exhausted_index_retries_fail_the_request(brain: dict) -> None:
    _ingest(brain, _token())
    job = _request_tick(brain)

    _drain(
        brain,
        embeddings_url=f"http://127.0.0.1:{_free_port()}",
        extra={"TICK_INDEX_MAX_ATTEMPTS": "1"},
    )

    status = _status(brain, job)
    assert status["status"] == "failed"
    assert status["index_attempts"] == 1
    assert status["error_tail"]


def test_missing_embeddings_key_is_not_completed(brain: dict) -> None:
    _ingest(brain, _token())
    job = _request_tick(brain)

    _drain(brain, extra={"EMBEDDINGS_API_KEY": ""})

    assert _status(brain, job)["status"] == "pending"


def test_dry_run_tick_leaves_vault_and_index_untouched(brain: dict) -> None:
    token = _token()
    _ingest(brain, token)
    before = _vault_digest(brain["vault"])
    job = _request_tick(brain, dry_run="true")

    result = _drain(brain)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _status(brain, job)["status"] == "completed"
    assert _vault_digest(brain["vault"]) == before
    assert _rows(brain, token) == []


def test_scheduled_vault_cycle_runs_the_same_pipeline(brain: dict) -> None:
    token = _token()
    _ingest(brain, token)

    result = _run_job(
        "templates/cronjob.yaml",
        brain["vault"],
        {"EMBEDDINGS_URL": brain["embeddings"]["url"], "EMBEDDINGS_API_KEY": EMBED_BEARER},
        ["--set", "cycleMode=vault"],
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "extraction" not in result.stdout.lower()
    assert "amygdala" not in result.stdout.lower()
    producers = {hit["producer"] for hit in _semantic_hits(brain, token)}
    assert {"brain-arc", "brain-lesson"} <= producers, producers


def test_chart_default_keeps_the_workstation_mode_explicit() -> None:
    script, env = _job_script("templates/cronjob.yaml", [])

    assert env["TICK_CYCLE_MODE"] == "workstation"
    assert "tick_cycle.py scheduled" in script


def test_unknown_cycle_mode_refuses_to_render() -> None:
    rendered = subprocess.run(
        ["helm", "template", "brain", str(CHART), "--set", "cycleMode=personal"],
        capture_output=True,
        text=True,
    )
    assert rendered.returncode != 0
    assert "cycleMode" in rendered.stderr
