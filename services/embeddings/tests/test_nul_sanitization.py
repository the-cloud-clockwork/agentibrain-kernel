from contextlib import nullcontext

import db


class FakeConnection:
    def __init__(self):
        self.calls = []

    def execute(self, query, params):
        self.calls.append((query, params))

    def commit(self):
        pass


class FakePool:
    def __init__(self, connection):
        self._connection = connection

    def connection(self):
        return nullcontext(self._connection)


def test_upsert_strips_nul_from_postgres_text(monkeypatch):
    connection = FakeConnection()
    monkeypatch.setattr(db, "get_pool", lambda: FakePool(connection))

    db.upsert_chunks(
        "key\x00",
        "brain-arc",
        "arc",
        [
            {
                "chunk_idx": 0,
                "text_preview": "text\x00value",
                "metadata": {"title\x00": "value\x00", "nested": ["item\x00"]},
                "embedding": [0.1, 0.2],
            }
        ],
    )

    params = connection.calls[1][1]
    assert params[0] == "key"
    assert params[4] == "textvalue"
    assert params[5].obj == {"title": "value", "nested": ["item"]}
