import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CYCLE = ROOT / "services/brain-ops/tick_cycle.py"
CHART = ROOT / "helm/brain-ops"

WORKER = r'''
import importlib.util
import sys
import time
from pathlib import Path
spec = importlib.util.spec_from_file_location("tick_cycle", sys.argv[1])
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)
vault = Path(sys.argv[2])
pause = sys.argv[4]
child = """
import sys, time
from pathlib import Path
vault, phase, pause = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
with (vault / 'events').open('a') as f:
    f.write(phase + '\\n')
(vault / (phase + '.entered')).touch()
while phase == pause and not (vault / 'release').exists():
    time.sleep(.01)
sys.exit(int(phase == 'maintenance' and (vault / 'fail').exists()))
"""
script = vault / 'maintenance.py'
script.write_text(child)
def maintenance(vault, flags, source):
    return m._run([str(script), str(vault), 'maintenance', pause], checkpoint=m.MAINTENANCE_RECEIPT.get())
def index(vault):
    return m._run(['-c', child, str(vault), 'index', pause])
finish = m._finish
def publish(request, data, dest):
    saved = finish(request, data, dest)
    if pause == 'publication' and dest.name == 'completed':
        (vault / 'publication.entered').touch()
        while not (vault / 'release').exists():
            time.sleep(.01)
    return saved
m.maintenance = maintenance
m.index = index
m._finish = publish
sys.exit(m.main([sys.argv[3], '--vault', str(vault), '--mode', 'vault']))
'''


def _request(vault, name="request"):
    requested = vault / "brain-feed/ticks/requested"
    requested.mkdir(parents=True, exist_ok=True)
    path = requested / f"{name}.json"
    path.write_text(json.dumps({"no_ai": True}))
    return path


@pytest.fixture
def workers():
    processes = []

    def launch(vault, cycle="drain", pause=""):
        vault.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen(
            [sys.executable, "-c", WORKER, str(CYCLE), str(vault), cycle, pause],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        processes.append(proc)
        return proc

    yield launch
    for proc in processes:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate(timeout=5)


def _wait(vault, phase, proc):
    deadline = time.monotonic() + 5
    while not (vault / f"{phase}.entered").exists():
        if proc.poll() is not None or time.monotonic() > deadline:
            pytest.fail(f"Worker failed to enter {phase}: {proc.communicate(timeout=1)}")
        time.sleep(0.01)


def _success(proc):
    output = proc.communicate(timeout=5)
    assert proc.returncode == 0, output
    return "".join(output)


def _released(vault):
    code = "import fcntl,sys; f=open(sys.argv[1], 'a+b'); fcntl.flock(f, fcntl.LOCK_EX)"
    subprocess.run(
        [sys.executable, "-c", code, str(vault / "brain-feed/ticks/.writer.lock")],
        timeout=5,
        check=True,
    )


@pytest.mark.parametrize("owner", ["scheduled", "drain"])
@pytest.mark.parametrize("pause", ["maintenance", "index"])
@pytest.mark.parametrize("contender", ["scheduled", "drain"])
def test_callers_share_ownership_through_index(workers, tmp_path, owner, pause, contender):
    request = _request(tmp_path)
    proc = workers(tmp_path, owner, pause)
    _wait(tmp_path, pause, proc)
    original = request.read_bytes()
    events = (tmp_path / "events").read_bytes()
    output = _success(workers(tmp_path, contender))
    assert "busy" in output
    assert request.read_bytes() == original
    assert (tmp_path / "events").read_bytes() == events
    (tmp_path / "release").touch()
    _success(proc)
    _success(workers(tmp_path))
    assert not request.exists()
    assert len(list((tmp_path / "brain-feed/ticks/completed").glob("*.json"))) == 1


def test_publication_is_owned_and_partial_publication_recovers(workers, tmp_path):
    first = _request(tmp_path, "first")
    second = _request(tmp_path, "second")
    proc = workers(tmp_path, pause="publication")
    _wait(tmp_path, "publication", proc)
    assert not first.exists()
    before = second.read_bytes()
    assert "busy" in _success(workers(tmp_path, "scheduled"))
    assert "busy" in _success(workers(tmp_path))
    assert second.read_bytes() == before
    proc.kill()
    proc.communicate(timeout=5)
    _success(workers(tmp_path))
    assert not second.exists()
    assert (tmp_path / "events").read_text().splitlines().count("maintenance") == 1
    completed = tmp_path / "brain-feed/ticks/completed"
    assert json.loads((completed / "first.json").read_text())["attempts"] == 1
    assert json.loads((completed / "second.json").read_text())["attempts"] == 2


def test_dead_parent_keeps_child_owned_then_resumes_index(workers, tmp_path):
    request = _request(tmp_path)
    proc = workers(tmp_path, pause="index")
    _wait(tmp_path, "index", proc)
    before = request.read_bytes()
    assert json.loads(before)["maintenance"] == "done"
    proc.kill()
    proc.wait(timeout=5)
    assert "busy" in _success(workers(tmp_path))
    assert request.read_bytes() == before
    (tmp_path / "release").touch()
    proc.communicate(timeout=5)
    _success(workers(tmp_path))
    assert not request.exists()
    assert (tmp_path / "events").read_text().splitlines().count("maintenance") == 1


def test_dead_parent_maintenance_child_checkpoints_before_recovery(workers, tmp_path):
    request = _request(tmp_path)
    proc = workers(tmp_path, pause="maintenance")
    _wait(tmp_path, "maintenance", proc)
    proc.kill()
    proc.wait(timeout=5)
    assert "busy" in _success(workers(tmp_path))
    (tmp_path / "release").touch()
    proc.communicate(timeout=5)
    _released(tmp_path)
    data = json.loads(request.read_text())
    receipt = tmp_path / "brain-feed/ticks" / f"{data['maintenance_receipt']}.receipt"
    assert receipt.exists()
    _success(workers(tmp_path))
    assert not request.exists()
    assert (tmp_path / "events").read_text().splitlines().count("maintenance") == 1


def _orphan_maintenance(workers, vault, fail=False):
    proc = workers(vault, pause="maintenance")
    _wait(vault, "maintenance", proc)
    proc.kill()
    proc.wait(timeout=5)
    if fail:
        (vault / "fail").touch()
    (vault / "release").touch()
    proc.communicate(timeout=5)
    _released(vault)


def test_failed_orphan_maintenance_publishes_no_receipt_and_reruns(workers, tmp_path):
    request = _request(tmp_path)
    _orphan_maintenance(workers, tmp_path, fail=True)
    data = json.loads(request.read_text())
    assert "maintenance" not in data
    assert not list((tmp_path / "brain-feed/ticks").glob("*.receipt"))
    (tmp_path / "fail").unlink()
    _success(workers(tmp_path))
    assert not request.exists()
    assert (tmp_path / "events").read_text().splitlines().count("maintenance") == 2
    completed = tmp_path / "brain-feed/ticks/completed/request.json"
    assert json.loads(completed.read_text())["attempts"] == 2


@pytest.mark.parametrize("partial", [False, True])
def test_coalesced_requests_recover_from_one_receipt(workers, tmp_path, partial):
    first = _request(tmp_path, "first")
    second = _request(tmp_path, "second")
    _orphan_maintenance(workers, tmp_path)
    identities = {json.loads(p.read_text())["maintenance_receipt"] for p in (first, second)}
    assert len(identities) == 1
    assert len(list((tmp_path / "brain-feed/ticks").glob("*.receipt"))) == 1
    if partial:
        first.write_text(json.dumps({**json.loads(first.read_text()), "maintenance": "done"}))
    _success(workers(tmp_path))
    assert not first.exists() and not second.exists()
    assert (tmp_path / "events").read_text().splitlines().count("maintenance") == 1
    assert len(list((tmp_path / "brain-feed/ticks/completed").glob("*.json"))) == 2
    assert not list((tmp_path / "brain-feed/ticks").glob("*.receipt"))


def test_replayed_deterministic_maintenance_accepts_each_write_once(tmp_path):
    inbox = tmp_path / "raw/inbox"
    inbox.mkdir(parents=True)
    (inbox / "probe.md").write_text("---\ntitle: Probe\ntags: [lesson]\n---\n\nProbe body.\n")
    env = {k: v for k, v in os.environ.items() if k not in ("REDIS_URL", "CLICKHOUSE_URL")}
    env["INFERENCE_URL"] = ""
    tick = ROOT / "services/brain-ops/brain_tick.py"
    argv = [sys.executable, str(tick), "--vault", str(tmp_path), "--no-ai"]
    argv += ["--brain-feed", str(tmp_path / "brain-feed")]
    files = []
    for _ in range(2):
        subprocess.run(argv, env=env, capture_output=True, check=True, timeout=60)
        files.append(sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p.is_file()))
    assert files[0] == files[1]
    assert [p for p in files[1] if p.name.startswith("probe")] == [Path("left/probe.md")]


def test_killed_process_group_releases_ownership_and_keeps_request(workers, tmp_path):
    request = _request(tmp_path)
    proc = workers(tmp_path, pause="maintenance")
    _wait(tmp_path, "maintenance", proc)
    os.killpg(proc.pid, signal.SIGKILL)
    proc.communicate(timeout=5)
    assert request.exists()
    _success(workers(tmp_path))
    assert not request.exists()
    assert len(list((tmp_path / "brain-feed/ticks/completed").glob("*.json"))) == 1


def test_distinct_vaults_are_independent(workers, tmp_path):
    left, right = tmp_path / "left", tmp_path / "right"
    _request(left)
    request = _request(right)
    proc = workers(left, pause="index")
    _wait(left, "index", proc)
    _success(workers(right))
    assert not request.exists()
    (left / "release").touch()
    _success(proc)


def _render(release, *values):
    rendered = subprocess.run(
        ["helm", "template", release, str(CHART), *values],
        capture_output=True,
        text=True,
        check=True,
    )
    return list(yaml.safe_load_all(rendered.stdout))


def test_release_resources_and_selectors_are_disjoint():
    left = _render("left", "--set", "amygdala.enabled=true")
    right = _render("right", "--set", "amygdala.enabled=true")
    assert {d["metadata"]["name"] for d in left}.isdisjoint({d["metadata"]["name"] for d in right})
    for docs in (left, right):
        assert {d["kind"] for d in docs} == {"CronJob", "Deployment"}
        assert len({d["metadata"]["name"] for d in docs if d["kind"] == "CronJob"}) == 2
        deployment = next(d for d in docs if d["kind"] == "Deployment")
        labels = deployment["spec"]["template"]["metadata"]["labels"]
        assert deployment["spec"]["selector"]["matchLabels"] == labels
    assert left[0]["spec"]["selector"] != right[0]["spec"]["selector"]


def test_explicit_fullname_keeps_distinct_cronjob_suffixes():
    docs = _render("brain", "--set", "fullnameOverride=custom")
    names = {d["metadata"]["name"] for d in docs}
    assert "custom" in names
    assert "custom-tick-drain" in names
    docs = _render("brain", "--set", "fullnameOverride=" + "x" * 63)
    names = [d["metadata"]["name"] for d in docs]
    assert len(names) == len(set(names))
    assert all(len(name) <= 52 for name in names)


@pytest.fixture
def cycle_module():
    spec = importlib.util.spec_from_file_location("tick_cycle_lock_test", CYCLE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("options", ["nolock", "local_lock=all", "local_lock=flock"])
def test_unsafe_nfs_mount_refuses_before_mutation(cycle_module, monkeypatch, tmp_path, options):
    request = _request(tmp_path)
    before = request.read_bytes()
    mountinfo = f"1 0 0:1 / / rw - ext4 disk rw\n2 1 0:2 / {tmp_path} rw - nfs4 server:/vault rw,{options}\n"
    monkeypatch.setattr(Path, "read_text", lambda self: mountinfo)
    with pytest.raises(RuntimeError, match="server coordinated NFS locking"):
        cycle_module.drain(tmp_path)
    assert request.read_bytes() == before
    assert not (tmp_path / "brain-feed/ticks/.writer.lock").exists()


@pytest.mark.parametrize("options", ["local_lock=none", "local_lock=posix"])
def test_server_coordinated_nfs_mount_is_accepted(cycle_module, monkeypatch, tmp_path, options):
    mountinfo = f"1 0 0:1 / / rw - ext4 disk rw\n2 1 0:2 / {tmp_path} rw - nfs4 server:/vault rw,{options}\n"
    monkeypatch.setattr(Path, "read_text", lambda self: mountinfo)
    cycle_module._qualify_mount(tmp_path)


def test_failure_closes_descriptor_without_replacing_lock(cycle_module, tmp_path):
    @cycle_module._owned
    def broken(vault):
        assert cycle_module.WRITER_FDS.get()
        raise RuntimeError("failed cycle")

    with pytest.raises(RuntimeError, match="failed cycle"):
        broken(tmp_path)
    lock = tmp_path / "brain-feed/ticks/.writer.lock"
    inode = lock.stat().st_ino
    assert cycle_module.WRITER_FDS.get() == ()
    assert cycle_module.drain(tmp_path) == 0
    assert lock.stat().st_ino == inode


def test_long_instance_names_remain_disjoint():
    left = _render("x" * 45 + "a")
    right = _render("x" * 45 + "b")
    assert {d["metadata"]["name"] for d in left}.isdisjoint({d["metadata"]["name"] for d in right})
