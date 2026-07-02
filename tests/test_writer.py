"""Unit tests for shared.opensearch.writer — bulk index + wipe semantics."""

from shared.opensearch.writer import wipe_paper


class _FakeIndices:
    def __init__(self, exists: bool) -> None:
        self._exists = exists
        self.exists_calls: list[str] = []

    def exists(self, index: str) -> bool:
        self.exists_calls.append(index)
        return self._exists


class _FakeClient:
    def __init__(self, exists: bool, deleted: int = 0) -> None:
        self.indices = _FakeIndices(exists)
        self._deleted = deleted
        self.delete_calls: list[dict] = []

    def delete_by_query(self, *, index: str, body: dict, refresh: bool) -> dict:
        self.delete_calls.append(
            {"index": index, "body": body, "refresh": refresh},
        )
        return {"deleted": self._deleted}


def test_wipe_paper_no_op_when_index_missing():
    client = _FakeClient(exists=False)
    deleted = wipe_paper(client, "chunks-v1", "paper-x")
    assert deleted == 0
    assert client.delete_calls == []           # no query issued
    assert client.indices.exists_calls == ["chunks-v1"]


def test_wipe_paper_filters_by_paper_id_and_refreshes():
    client = _FakeClient(exists=True, deleted=17)
    deleted = wipe_paper(client, "chunks-v1", "paper-x")
    assert deleted == 17
    assert len(client.delete_calls) == 1
    call = client.delete_calls[0]
    assert call["index"] == "chunks-v1"
    assert call["body"] == {"query": {"term": {"paper_id": "paper-x"}}}
    # refresh=True is critical — without it, deletes race the subsequent bulk write.
    assert call["refresh"] is True


def test_wipe_paper_returns_int_even_when_response_missing_key():
    class _Client(_FakeClient):
        def delete_by_query(self, **kwargs):
            self.delete_calls.append(kwargs)
            return {}                       # some clients omit "deleted" on 0-hit
    client = _Client(exists=True)
    assert wipe_paper(client, "chunks-v1", "paper-x") == 0


def _run_all():
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    return len(tests)


if __name__ == "__main__":
    n = _run_all()
    print(f"PASS: {n} writer tests")
