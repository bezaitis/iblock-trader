"""SQLite state: key/value flags, HTTP cache, and per-cycle snapshots."""

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS http_cache (url TEXT PRIMARY KEY, fetched_at REAL NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS snapshots (
    ts TEXT NOT NULL, market_id INTEGER NOT NULL, title TEXT, yes_price REAL,
    p_yes REAL, confidence TEXT, data_ok INTEGER, notes TEXT, inputs TEXT
);
CREATE TABLE IF NOT EXISTS alerts (ts TEXT NOT NULL, level TEXT, title TEXT, body TEXT);
CREATE TABLE IF NOT EXISTS orders (
    ts TEXT NOT NULL, mode TEXT, market_id INTEGER, side TEXT, outcome TEXT, amount REAL, amount_type TEXT,
    status TEXT, reason TEXT, quote TEXT, trade TEXT
);
"""


class Store:
    def __init__(self, path: Path | str):
        self.db = sqlite3.connect(str(path))
        self.db.executescript(SCHEMA)

    def get(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: str, value) -> None:
        self.db.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, json.dumps(value)))
        self.db.commit()

    def cache_get(self, url: str) -> tuple[str, float] | None:
        return self.db.execute("SELECT body, fetched_at FROM http_cache WHERE url = ?", (url,)).fetchone()

    def cache_put(self, url: str, body: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO http_cache VALUES (?, ?, ?)", (url, time.time(), body))
        self.db.commit()

    def snapshot(self, ts: str, market: dict, result) -> None:
        self.db.execute(
            "INSERT INTO snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, market["id"], market.get("title"), market.get("yesPrice"), result.p_yes,
             result.confidence, int(result.data_ok), result.notes, json.dumps(result.inputs, default=str)),
        )
        self.db.commit()

    def log_alert(self, ts: str, level: str, title: str, body: str) -> None:
        self.db.execute("INSERT INTO alerts VALUES (?, ?, ?, ?)", (ts, level, title, body))
        self.db.commit()

    def log_order(self, ts: str, mode: str, order, status: str, reason, quote, trade) -> None:
        self.db.execute(
            "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, mode, order.market_id, order.side, order.outcome, order.amount, order.amount_type,
             status, reason, quote, trade),
        )
        self.db.commit()
