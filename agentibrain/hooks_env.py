"""The agentihooks side of the brain's configuration.

The kernel and agentihooks resolve their config from different files. The
kernel reads ``~/.agentibrain/.env``; agentihooks reads ``~/.agentihooks/.env``
and the ``*.env`` companions beside it, and nothing bridges the two. A token
written only to the kernel's file authenticates every ``agentibrain`` command
while every marker POST from the hook answers 401 — the deployment looks
healthy from the side that owns the check.

``agentibrain install`` is the only writer of the managed file. Resolution
mirrors agentihooks' own loader: ``.env`` first, then ``*.env`` sorted, later
files winning, and the surrounding process environment beating all of them.
The managed name sorts after ``.env`` and before ``zz-*.env``, so an operator
keeps a companion file that overrides it.
"""

from __future__ import annotations

import os
from pathlib import Path

MANAGED_ENV_NAME = "agentibrain.env"
OUTBOX_DIRS = ("brain-outbox", "brain-outbox-backlog")

_HEADER = """\
# Managed by `agentibrain install` — rewritten in full on every run.
# Override in a later-sorting companion (~/.agentihooks/zz-*.env) or the
# process environment; both beat this file in agentihooks' loader.
"""


def hooks_home() -> Path:
    return Path(os.environ.get("AGENTIHOOKS_HOME", str(Path.home() / ".agentihooks"))).expanduser()


def managed_env_path() -> Path:
    return hooks_home() / MANAGED_ENV_NAME


def ensure_outbox_dirs() -> list[Path]:
    """Create the marker buffers user-owned.

    compose binds ``~/.agentihooks`` into the tick container. Absent at first
    ``up``, the Docker daemon creates it root:root and the writer can never
    buffer a marker again.
    """
    created = []
    for name in OUTBOX_DIRS:
        d = hooks_home() / name
        d.mkdir(parents=True, exist_ok=True)
        created.append(d)
    return created


def write_hooks_env(*, brain_url: str, token: str) -> Path:
    """Publish the reader/writer config into the agentihooks env chain."""
    path = managed_env_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        _HEADER,
        f"BRAIN_URL={brain_url}",
        f"BRAIN_HTTP_TOKEN={token}",
        "BRAIN_ENABLED=true",
        "BRAIN_WRITER_ENABLED=true",
        f"BRAIN_WRITER_OUTBOX={hooks_home() / OUTBOX_DIRS[0]}",
    ]
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)
    return path


def _parse_chain() -> dict[str, str]:
    """agentihooks' load order, for when agentihooks is not importable."""
    home = hooks_home()
    resolved: dict[str, str] = {}
    files = [home / ".env"]
    if home.is_dir():
        files += [f for f in sorted(home.glob("*.env")) if f.name != ".env"]
    for env_file in files:
        if not env_file.is_file():
            continue
        seen: set[str] = set()
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
                value = value[1:-1]
            elif "#" in value:
                value = value[: value.index("#")].rstrip()
            if not key or key in seen:
                continue
            seen.add(key)
            resolved[key] = value
    return resolved


def _truthy(value: str) -> bool:
    return value.strip().lower() in ("true", "1", "yes")


def resolve_consumer_config() -> dict:
    """What agentihooks itself would resolve on this machine.

    Imports agentihooks when it is importable, so the answer comes from the
    consumer's own resolution rather than a second implementation of it. The
    file-chain parse is the fallback for a kernel installed without it.

    ``token`` is returned so a caller can authenticate a probe with it. It is
    never rendered — callers display ``token_present``.
    """
    try:
        from hooks import config as hooks_config

        hooks_config.reload_brain_env(force=True)
        token = hooks_config.BRAIN_HTTP_TOKEN
        return {
            "source": "agentihooks",
            "brain_url": hooks_config.BRAIN_URL,
            "reader_enabled": bool(hooks_config.BRAIN_ENABLED),
            "writer_enabled": bool(hooks_config.BRAIN_WRITER_ENABLED),
            "token": token,
            "token_present": bool(token),
        }
    except Exception:  # noqa: BLE001 — agentihooks absent or mid-upgrade
        pass

    env = _parse_chain()

    def _get(key: str) -> str:
        return os.environ.get(key) or env.get(key, "")

    url = _get("BRAIN_URL").rstrip("/")
    token = _get("BRAIN_HTTP_TOKEN") or _get("KB_ROUTER_TOKEN")
    default = "true" if url else "false"
    return {
        "source": "env-chain",
        "brain_url": url,
        "reader_enabled": _truthy(_get("BRAIN_ENABLED") or default),
        "writer_enabled": _truthy(_get("BRAIN_WRITER_ENABLED") or default),
        "token": token,
        "token_present": bool(token),
    }
