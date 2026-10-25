"""Live harness for the issue 89 acceptance suite.

Real brain-api and embeddings processes on pgvector, the brain-ops extract,
cluster, outbox drain and tick drain scripts run as the deployment runs them,
and real git checkouts with linked worktrees. Only the upstream model is a
local stand-in: it embeds by word hashing and answers the tick prompt's
project intent task from the active project list the tick itself supplied.

Needs TICK_CYCLE_POSTGRES_URL (a pgvector server whose user may create
databases); CI provides it.
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
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[3]
BRAIN_OPS = ROOT / "services" / "brain-ops"
POSTGRES_URL = os.environ.get("TICK_CYCLE_POSTGRES_URL", "")
DATABASE = "project_scope_acceptance"
DIM = 256
BRAIN_ID = "swarm-acceptance"
EMBED_BEARER = uuid.uuid4().hex
ROUTER = uuid.uuid4().hex

requires_live_stack = pytest.mark.skipif(
    not os.environ.get("CI") and not POSTGRES_URL, reason="needs TICK_CYCLE_POSTGRES_URL"
)


def vector(text: str) -> list[float]:
    vec = [0.0] * DIM
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        vec[int(hashlib.sha256(word.encode()).hexdigest(), 16) % DIM] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def intent_answer(prompt: str) -> str:
    listed = prompt.split("Active projects:", 1)[-1].split("Format", 1)[0]
    projects = [line[2:].strip() for line in listed.splitlines() if line.startswith("- ")]
    lines = [
        f"PROJECT_INTENT: {p} | Shipping the {p.rsplit('/', 1)[-1]} fixture work" for p in projects
    ]
    return "### 7. Project Intent\n" + ("\n".join(lines) or "None.")


class _ModelHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path.endswith("/chat/completions"):
            answer = intent_answer(body["messages"][-1]["content"])
            data = {"choices": [{"message": {"content": answer}}]}
        else:
            data = {"data": [{"embedding": vector(body["input"])}]}
        payload = json.dumps(data).encode()
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


def serve(cwd: Path, module: str, env: dict) -> tuple[subprocess.Popen, str]:
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


def stop(proc: subprocess.Popen) -> None:
    proc.terminate()
    proc.wait(timeout=10)


def git(cwd: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    }
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True, env=env
    ).stdout.strip()


@dataclass
class Project:
    key: str
    remote: str
    project_id: str
    word: str
    checkout: Path = Path()
    worktree: Path = Path()
    token: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass
class Fixture:
    name: str
    alpha: Project
    beta: Project
    fleet_warning: str
    unattributed_lesson: str
    manifest: dict = field(default_factory=dict)

    @property
    def projects(self) -> tuple[Project, Project]:
        return (self.alpha, self.beta)


FIXTURE_SETS = {
    "one": {
        "alpha": ("https://github.com/Acme/Alpha-Repo.git", "github.com/acme/alpha-repo", "quorum"),
        "beta": ("git@gitlab.example:team/beta-svc.git", "gitlab.example/team/beta-svc", "tidal"),
        "fleet": "Fleet one: the shared runner pool is at capacity.",
    },
    "two": {
        "alpha": (
            "https://github.com/Globex/Gamma-App.git",
            "github.com/globex/gamma-app",
            "lantern",
        ),
        "beta": (
            "ssh://git@git.example.org/ops/delta-api.git",
            "git.example.org/ops/delta-api",
            "glacier",
        ),
        "fleet": "Fleet two: the shared object store is read only.",
    },
}


def _repository(base: Path, project: Project) -> None:
    project.checkout = base / project.key
    project.checkout.mkdir(parents=True)
    git(project.checkout, "init", "-q", "-b", "dev")
    (project.checkout / "README.md").write_text(f"{project.key}\n", encoding="utf-8")
    git(project.checkout, "add", "README.md")
    git(project.checkout, "commit", "-q", "-m", "seed")
    git(project.checkout, "remote", "add", "origin", project.remote)
    project.worktree = base / "worktrees" / project.key / f"{project.key}-task"
    git(
        project.checkout,
        "worktree",
        "add",
        "-q",
        "-b",
        f"{project.key}-task",
        str(project.worktree),
    )


def _iso(when: datetime) -> str:
    return when.isoformat(timespec="seconds").replace("+00:00", "Z")


def write_claude_session(home: Path, session_id: str, cwd: Path, branch: str, prompts: list[str]):
    folder = home / re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
    folder.mkdir(parents=True, exist_ok=True)
    start = datetime.now(timezone.utc) - timedelta(minutes=40)
    lines = []
    for i, prompt in enumerate(prompts):
        stamp = _iso(start + timedelta(minutes=2 * i))
        lines.append(
            {
                "type": "user",
                "timestamp": stamp,
                "cwd": str(cwd),
                "gitBranch": branch,
                "message": {"role": "user", "content": prompt},
            }
        )
        lines.append(
            {
                "type": "assistant",
                "timestamp": stamp,
                "cwd": str(cwd),
                "message": {"content": [{"type": "text", "text": f"Done: {prompt}"}]},
            }
        )
    path = folder / f"{session_id}.jsonl"
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    return path


def write_codex_session(
    home: Path, session_id: str, cwd: Path, branch: str, remote: str, prompts: list[str]
):
    start = datetime.now(timezone.utc) - timedelta(minutes=35)
    folder = home / start.strftime("%Y/%m/%d")
    folder.mkdir(parents=True, exist_ok=True)
    meta = {
        "id": session_id,
        "timestamp": _iso(start),
        "cwd": str(cwd),
        "originator": "codex_cli_rs",
        "git": {"branch": branch, "commit_hash": "0" * 40, "repository_url": remote},
    }
    lines = [{"timestamp": _iso(start), "type": "session_meta", "payload": meta}]
    for i, prompt in enumerate(prompts):
        stamp = _iso(start + timedelta(minutes=2 * i))
        lines += [
            {
                "timestamp": stamp,
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": prompt}],
                },
            },
            {
                "timestamp": stamp,
                "type": "response_item",
                "payload": {"type": "function_call", "name": "shell", "arguments": "{}"},
            },
            {
                "timestamp": stamp,
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": f"Done: {prompt}"}],
                },
            },
        ]
    name = f"rollout-{start.strftime('%Y-%m-%dT%H-%M-%S')}-{session_id}.jsonl"
    path = folder / name
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    return path


def _build_fixture(tmp_path: Path, name: str) -> Fixture:
    spec = FIXTURE_SETS[name]
    alpha = Project("alpha", spec["alpha"][0], spec["alpha"][1], spec["alpha"][2])
    beta = Project("beta", spec["beta"][0], spec["beta"][1], spec["beta"][2])
    repos = tmp_path / "repos"
    for project in (alpha, beta):
        _repository(repos, project)
    claude = tmp_path / "claude-projects"
    codex = tmp_path / "codex-sessions"
    sessions = [
        write_claude_session(
            claude,
            f"claude-{alpha.word}",
            alpha.checkout,
            "dev",
            [f"Fix the {alpha.word} ledger retry in the main checkout", "Run the checks again"],
        ),
        write_codex_session(
            codex,
            f"codex-{alpha.word}",
            alpha.worktree,
            "alpha-task",
            alpha.remote,
            [f"Add the {alpha.word} worktree export from codex", "Push the branch"],
        ),
        write_codex_session(
            codex,
            f"codex-{beta.word}",
            beta.checkout,
            "dev",
            beta.remote,
            [f"Tune the {beta.word} queue consumer from codex", "Measure it again"],
        ),
        write_claude_session(
            claude,
            f"claude-{beta.word}",
            beta.worktree,
            "beta-task",
            [f"Document the {beta.word} worktree rollback", "Open the pull request"],
        ),
    ]
    manifest = {
        "fixture": name,
        "inputs": {
            str(p.relative_to(tmp_path)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sessions
        },
        "projects": {p.key: p.project_id for p in (alpha, beta)},
    }
    return Fixture(
        name,
        alpha,
        beta,
        spec["fleet"],
        f"Unattributed lesson: the {alpha.word} {beta.word} words appear here with no project.",
        manifest,
    )


def _database_url() -> str:
    import psycopg

    with psycopg.connect(POSTGRES_URL, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {DATABASE} WITH (FORCE)")
        conn.execute(f"CREATE DATABASE {DATABASE}")
    return POSTGRES_URL.rsplit("/", 1)[0] + f"/{DATABASE}"


@pytest.fixture(scope="session")
def model():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ModelHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture(scope="session")
def embeddings(model: str):
    db_url = _database_url()
    proc, url = serve(
        ROOT / "services" / "embeddings" / "src",
        "main:app",
        {
            "POSTGRES_URL": db_url,
            "LLM_API_BASE": model,
            "LLM_API_KEY": uuid.uuid4().hex,
            "LLM_EMBED_MODEL": "stand-in",
            "EMBED_DIM": str(DIM),
            "AUTH_MODE": "required",
            "API_KEYS": EMBED_BEARER,
        },
    )
    yield {"url": url, "db": db_url}
    stop(proc)


class Brain:
    """One fixture's brain: its vault, its brain-api process and the client calls it serves."""

    def __init__(self, tmp_path: Path, fixture: Fixture, embeddings: dict, model: str):
        from agentibrain.scaffold import scaffold

        self.tmp = tmp_path
        self.fixture = fixture
        self.embeddings = embeddings
        self.model = model
        self.vault = tmp_path / "vault"
        scaffold(self.vault)
        self.proc: subprocess.Popen | None = None
        self.url = ""
        self.start()

    def start(self) -> None:
        grants = ",".join(f"{BRAIN_ID}@{p.project_id}={p.token}" for p in self.fixture.projects)
        self.proc, self.url = serve(
            ROOT / "services" / "brain-api",
            "app.main:app",
            {
                "VAULT_ROOT": str(self.vault),
                "KB_ROUTER_TOKENS": ROUTER,
                "KB_ROUTER_TOKEN": ROUTER,
                "BRAIN_ID": BRAIN_ID,
                "KB_WORKER_TOKENS": grants,
                "INFERENCE_URL": "",
                "EMBEDDINGS_URL": "",
            },
        )

    def restart(self) -> None:
        stop(self.proc)
        self.start()

    def close(self) -> None:
        stop(self.proc)

    def client(self, token: str = ROUTER) -> httpx.Client:
        return httpx.Client(
            base_url=self.url, headers={"Authorization": f"Bearer {token}"}, timeout=30
        )

    def extract(self) -> dict:
        out = self.vault / "clusters" / time.strftime("%Y-%m-%d", time.gmtime())
        extracted = subprocess.run(
            [
                sys.executable,
                str(BRAIN_OPS / "extract.py"),
                "--since",
                "2h",
                "--min-turns",
                "1",
                "--projects-dir",
                str(self.tmp / "claude-projects"),
                "--codex-dir",
                str(self.tmp / "codex-sessions"),
            ],
            capture_output=True,
            text=True,
        )
        assert extracted.returncode == 0, extracted.stderr
        clustered = subprocess.run(
            [sys.executable, str(BRAIN_OPS / "cluster.py"), "--out-dir", str(out)],
            input=extracted.stdout,
            capture_output=True,
            text=True,
        )
        assert clustered.returncode == 0, clustered.stderr
        return json.loads(extracted.stdout)

    def replay_outbox(self, entries: list[dict], token: str) -> subprocess.CompletedProcess:
        outbox = self.tmp / "brain-outbox"
        outbox.mkdir(exist_ok=True)
        for entry in entries:
            (outbox / f"{entry['idempotency_key']}.json").write_text(json.dumps(entry))
        return subprocess.run(
            [
                sys.executable,
                str(BRAIN_OPS / "outbox_drain.py"),
                "--outbox",
                str(outbox),
                "--brain-url",
                self.url,
                "--token",
                token,
            ],
            capture_output=True,
            text=True,
        )

    def tick(self) -> subprocess.CompletedProcess:
        job = self.client().post("/tick", params={"source": "issue89-acceptance"})
        assert job.status_code == 202, job.text
        base = {k: v for k, v in os.environ.items() if not k.startswith(("EMBED", "INFERENCE"))}
        result = subprocess.run(
            [sys.executable, str(BRAIN_OPS / "tick_cycle.py"), "drain", "--vault", str(self.vault)],
            env={
                **base,
                "REDIS_URL": "",
                "CLICKHOUSE_URL": "",
                "INFERENCE_URL": self.model,
                "EMBEDDINGS_URL": self.embeddings["url"],
                "EMBEDDINGS_API_KEY": EMBED_BEARER,
            },
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        status = self.client().get(f"/tick/{job.json()['job_id']}").json()
        assert status["status"] == "completed", (status, result.stdout)
        return result

    def search(self, query: str, **filters) -> list[dict]:
        resp = httpx.post(
            f"{self.embeddings['url']}/search",
            json={"query": query, "limit": 50, **filters},
            headers={"Authorization": f"Bearer {EMBED_BEARER}"},
            timeout=30,
        )
        assert resp.status_code == 200, resp.text
        return resp.json()["results"]

    def feed_files(self) -> dict[str, str]:
        feed = self.vault / "brain-feed"
        return {
            str(p.relative_to(feed)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(feed.rglob("*"))
            if p.is_file() and "ticks" not in p.relative_to(feed).parts
        }

    def knowledge_files(self) -> dict[str, str]:
        return {
            str(p.relative_to(self.vault)): hashlib.sha256(p.read_bytes()).hexdigest()
            for folder in ("left/reference", "left/decisions", "amygdala")
            for p in sorted((self.vault / folder).rglob("*.md"))
        }


@pytest.fixture(params=sorted(FIXTURE_SETS))
def brain(request, tmp_path: Path, embeddings: dict, model: str):
    import psycopg

    if shutil.which("git") is None:
        pytest.skip("needs git")
    with psycopg.connect(embeddings["db"], autocommit=True) as conn:
        conn.execute("TRUNCATE content_embeddings")
    fixture = _build_fixture(tmp_path, request.param)
    instance = Brain(tmp_path, fixture, embeddings, model)
    yield instance
    instance.close()
