import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.fixture
def amygdala():
    path = Path(__file__).resolve().parents[1] / "amygdala.py"
    spec = importlib.util.spec_from_file_location("amygdala_reconnect", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ConnectionLost(Exception):
    pass


class ResponseError(Exception):
    pass


class StreamStore:
    def __init__(self):
        self.entries = [
            (f"{n}-0", {"severity": "warning", "title": str(n), "event": "brain.test"})
            for n in range(1, 4)
        ]
        self.last_id = "0-0"
        self.pending = {}
        self.acked = []
        self.reads = []
        self.lost_response = False
        self.connections = []

    def connect(self, *args, **kwargs):
        client = Mock()
        client.xreadgroup.side_effect = self.read
        client.xack.side_effect = self.ack
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=False)
        self.connections.append((client, kwargs))
        return client

    def read(self, group, consumer, streams, **kwargs):
        cursor = streams["events:test"]
        self.reads.append(cursor)
        if cursor != ">":
            return [("events:test", list(self.pending.items()))]
        entries = [(key, fields) for key, fields in self.entries if key > self.last_id][:1]
        if not entries:
            raise KeyboardInterrupt
        key, fields = entries[0]
        self.last_id = key
        self.pending[key] = fields
        if key == "2-0" and not self.lost_response:
            self.lost_response = True
            raise ConnectionLost("read response lost after Redis recorded delivery")
        return [("events:test", entries)]

    def ack(self, stream, group, key):
        self.acked.append(key)
        del self.pending[key]


def prepare_loop(amygdala, monkeypatch):
    monkeypatch.setattr(amygdala.sys, "stdout", Mock())
    monkeypatch.setattr(amygdala.sys, "stderr", Mock())
    sleep = Mock()
    monkeypatch.setattr(amygdala.time, "sleep", sleep)
    return sleep


def test_lost_read_reconnects_and_drains_pending_without_loss_or_repeat(
    amygdala, monkeypatch, tmp_path
):
    store = StreamStore()
    monkeypatch.setattr(amygdala, "STREAMS", ["events:test"])
    monkeypatch.setattr(
        amygdala,
        "redis",
        SimpleNamespace(
            Redis=SimpleNamespace(from_url=store.connect),
            retry=SimpleNamespace(Retry=lambda backoff, retries: SimpleNamespace(retries=retries)),
            backoff=SimpleNamespace(NoBackoff=Mock()),
            exceptions=SimpleNamespace(
                ConnectionError=ConnectionLost,
                TimeoutError=ConnectionLost,
                ResponseError=ResponseError,
            ),
        ),
    )
    delivered = []
    monkeypatch.setattr(
        amygdala,
        "write_signal_file",
        lambda feed, events, dry_run: delivered.extend(event["msg_id"] for event in events) or True,
    )
    sleep = prepare_loop(amygdala, monkeypatch)

    amygdala.run_continuous("redis://unused/11", tmp_path, tmp_path)

    assert store.lost_response
    assert delivered == ["1-0", "2-0", "3-0"]
    assert store.acked == delivered
    assert store.pending == {}
    assert store.last_id == "3-0"
    assert len(store.connections) >= 4
    assert sleep.call_args_list[0].args == (1,)
    assert all(options["socket_connect_timeout"] == 5 for _, options in store.connections)
    assert all(options["socket_timeout"] == 5 for _, options in store.connections)
    assert all(options["retry"].retries == 0 for _, options in store.connections)


def test_connection_backoff_is_bounded_and_resets_after_recovery(amygdala, monkeypatch, tmp_path):
    outcomes = [ConnectionLost("offline")] * 7 + [
        {},
        ConnectionLost("offline"),
        KeyboardInterrupt(),
    ]
    consume = Mock(side_effect=outcomes)
    monkeypatch.setattr(amygdala, "consume", consume)
    monkeypatch.setattr(
        amygdala,
        "redis",
        SimpleNamespace(
            exceptions=SimpleNamespace(ConnectionError=ConnectionLost, TimeoutError=ConnectionLost)
        ),
    )
    sleep = prepare_loop(amygdala, monkeypatch)

    amygdala.run_continuous("redis://unused/11", tmp_path, tmp_path)

    assert [call.args[0] for call in sleep.call_args_list] == [1, 2, 4, 8, 16, 30, 30, 1]
    assert consume.call_count == 10
