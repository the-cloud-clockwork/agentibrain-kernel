import hashlib
import os
import secrets
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(shutil.which("sha256sum") is None, reason="Redis startup uses GNU coreutils")
def test_source_redis_recreates_named_auth_without_losing_legacy_access(tmp_path):
    bundle_value = secrets.token_urlsafe()
    acl = tmp_path / "users.acl"
    capture = tmp_path / "entrypoint-args"
    binary = tmp_path / "bin"
    binary.mkdir()
    entrypoint = binary / "docker-entrypoint.sh"
    entrypoint.write_text(
        '#!/usr/bin/env bash\nset -euo pipefail\nprintf "%s\\n" "$@" > "$CAPTURE"\n'
    )
    entrypoint.chmod(0o700)
    env = {
        **os.environ,
        "PATH": f"{binary}:{os.environ['PATH']}",
        "CAPTURE": str(capture),
        "SWARM_REDIS_PASSWORD": bundle_value,
    }
    script = ROOT / "local" / "redis" / "start.sh"
    expected = (
        "user default on nopass ~* &* +@all -flushall -flushdb\n"
        f"user swarm on #{hashlib.sha256(bundle_value.encode()).hexdigest()} ~* &* +@all -flushall -flushdb\n"
    )
    for _ in range(2):
        done = subprocess.run(
            ["bash", str(script), str(acl)], env=env, capture_output=True, text=True
        )
        assert done.returncode == 0, done.stderr
        assert acl.read_text() == expected
        assert stat.S_IMODE(acl.stat().st_mode) == 0o600
        assert capture.read_text().splitlines() == [
            "redis-server",
            "--appendonly",
            "yes",
            "--aclfile",
            str(acl),
        ]
        assert bundle_value not in done.stdout + done.stderr
    service = yaml.safe_load((ROOT / "compose.yml").read_text())["services"]["redis"]
    assert service["command"] == ["bash", "/compose/redis/start.sh"]
    assert "./local/redis:/compose/redis:ro" in service["volumes"]
    assert service["env_file"] == [
        {
            "path": "${SWARM_REDIS_ENV_FILE:-${HOME}/.agentihooks/swarm-stack/.env}",
            "required": False,
        }
    ]
