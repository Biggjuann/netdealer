"""Unit tests for the SQLite trade store — temp file, no network."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.store import TradeStore

C = "credit_spreads"


def _store():
    d = tempfile.mkdtemp()
    return TradeStore(os.path.join(d, "t.db"))


def test_put_list_get_order():
    s = _store()
    s.put(C, "AAPL|PUT|x", {"id": "AAPL|PUT|x", "status": "open", "ts": 100, "ticker": "AAPL"})
    s.put(C, "NVDA|CALL|x", {"id": "NVDA|CALL|x", "status": "open", "ts": 200, "ticker": "NVDA"})
    got = s.list(C)
    assert [r["ticker"] for r in got] == ["NVDA", "AAPL"]     # newest (ts desc) first
    print(f"ok  put/list ordered by ts desc ({len(got)} rows)")


def test_upsert_replaces():
    s = _store()
    s.put(C, "id1", {"id": "id1", "ts": 1, "entry_credit": 1.0})
    s.put(C, "id1", {"id": "id1", "ts": 2, "entry_credit": 1.2})
    rows = s.list(C)
    assert len(rows) == 1 and rows[0]["entry_credit"] == 1.2
    print("ok  upsert replaces by id")


def test_delete_and_clear_by_status():
    s = _store()
    s.put(C, "o1", {"id": "o1", "ts": 1, "status": "open"})
    s.put(C, "c1", {"id": "c1", "ts": 2, "status": "closed"})
    s.put(C, "c2", {"id": "c2", "ts": 3, "status": "closed"})
    assert s.delete(C, "o1") is True and s.delete(C, "nope") is False
    assert s.clear(C, "closed") == 2
    assert s.list(C) == []
    print("ok  delete + clear-by-status")


def test_separate_collections():
    s = _store()
    s.put(C, "a", {"id": "a", "ts": 1})
    s.put("other", "a", {"id": "a", "ts": 1})
    assert len(s.list(C)) == 1 and len(s.list("other")) == 1
    s.clear(C)
    assert s.list(C) == [] and len(s.list("other")) == 1
    print("ok  collections are isolated")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"\nAll {len(tests)} store tests passed.")


if __name__ == "__main__":
    main()
