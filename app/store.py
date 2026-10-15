"""Tiny SQLite-backed record store for server-side trade persistence.

One table of JSON records keyed by (collection, id). The credit-spread tracker
uses collection ``credit_spreads``. Point ``DB_PATH`` at a Railway *volume*
(e.g. /data/netdealer.db) so trades survive redeploys; without a volume the file
is ephemeral. Thread-safe for uvicorn's worker threads via a module lock.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from typing import List, Optional

log = logging.getLogger("store")


class TradeStore:
    def __init__(self, path: str):
        self.path = self._resolve(path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS records (
                collection TEXT NOT NULL,
                id         TEXT NOT NULL,
                ts         INTEGER NOT NULL,
                data       TEXT NOT NULL,
                PRIMARY KEY (collection, id)
            )""")
        self._conn.commit()
        log.info("trade store at %s", self.path)

    @staticmethod
    def _resolve(path: str) -> str:
        """Use ``path`` if its directory is writable; else fall back to a local
        (ephemeral) file so the app never crashes when no volume is mounted."""
        for candidate in (path, os.path.join(os.getcwd(), "netdealer.db")):
            d = os.path.dirname(candidate) or "."
            try:
                os.makedirs(d, exist_ok=True)
                if os.access(d, os.W_OK):
                    if candidate != path:
                        log.warning("DB_PATH %s not writable — falling back to %s "
                                    "(ephemeral; mount a volume for durability)", path, candidate)
                    return candidate
            except OSError:
                continue
        return ":memory:"

    def list(self, collection: str) -> List[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM records WHERE collection=? ORDER BY ts DESC",
                (collection,)).fetchall()
        out = []
        for (data,) in rows:
            try:
                out.append(json.loads(data))
            except json.JSONDecodeError:
                continue
        return out

    def put(self, collection: str, rec_id: str, data: dict) -> dict:
        ts = int(data.get("ts") or data.get("closed_ts") or time.time() * 1000)
        with self._lock:
            self._conn.execute(
                "INSERT INTO records (collection,id,ts,data) VALUES (?,?,?,?) "
                "ON CONFLICT(collection,id) DO UPDATE SET ts=excluded.ts, data=excluded.data",
                (collection, rec_id, ts, json.dumps(data)))
            self._conn.commit()
        return data

    def delete(self, collection: str, rec_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM records WHERE collection=? AND id=?", (collection, rec_id))
            self._conn.commit()
            return cur.rowcount > 0

    def clear(self, collection: str, status: Optional[str] = None) -> int:
        """Delete a whole collection, or only records whose data.status == status."""
        with self._lock:
            if status is None:
                cur = self._conn.execute("DELETE FROM records WHERE collection=?", (collection,))
                self._conn.commit()
                return cur.rowcount
            rows = self._conn.execute(
                "SELECT id,data FROM records WHERE collection=?", (collection,)).fetchall()
            ids = []
            for rid, data in rows:
                try:
                    if json.loads(data).get("status") == status:
                        ids.append(rid)
                except json.JSONDecodeError:
                    continue
            for rid in ids:
                self._conn.execute("DELETE FROM records WHERE collection=? AND id=?", (collection, rid))
            self._conn.commit()
            return len(ids)
