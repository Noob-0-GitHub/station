"""The ledger: every paid use (model calls, Tavily, the OpenAI API, ...) in one SQLite table.

`state/ledger.db`, WAL mode, so the loop and `station api` (calls and `--record`)
can write at the same time. Each row keeps the raw units (tokens, Tavily
credits, seconds of audio) next to the prices derived from them, so rows can
be repriced later:

- `list_usd`: the list price of what was used; NULL when it could not be priced
- `paid_usd`: what was actually paid; 0 on a subscription (Codex), NULL when unknown
- `credits`: what counts against the allowances (list price times `credits.factor`)

Before the ledger existed, usage was in `usage.jsonl` (and gzipped days under
`usage/`). `Store` imports those once and moves them to `usage-legacy/`.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

SCHEMA = 1
COLUMNS = ("ts", "service", "provider", "model", "kind", "thread", "what", "units", "tokens",
           "list_usd", "paid_usd", "credits", "source")
TABLE = """CREATE TABLE IF NOT EXISTS spend (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    service TEXT NOT NULL,
    provider TEXT,
    model TEXT,
    kind TEXT,
    thread TEXT,
    what TEXT,
    units TEXT NOT NULL DEFAULT '{}',
    tokens INTEGER,
    list_usd REAL,
    paid_usd REAL,
    credits REAL NOT NULL DEFAULT 0,
    source TEXT
)"""


class Ledger:
    def __init__(self, path):
        self.path = Path(path)
        with closing(self._connect()) as db:
            db.execute("PRAGMA journal_mode = WAL")
            if db.execute("PRAGMA user_version").fetchone()[0] < SCHEMA:
                with transaction(db):
                    db.execute(TABLE)
                    db.execute("CREATE INDEX IF NOT EXISTS spend_ts ON spend (ts)")
                    db.execute(f"PRAGMA user_version = {SCHEMA}")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.execute("PRAGMA busy_timeout = 30000")
        db.row_factory = sqlite3.Row
        return db

    # ---- writing ---------------------------------------------------------

    def add(self, service: str, *, credits: float = 0.0, units: dict | None = None, **fields) -> dict:
        row = {"ts": time.time(), "service": service, "credits": credits, **fields,
               "units": json.dumps(units or {}, ensure_ascii=False)}
        unknown = set(row) - set(COLUMNS)
        if unknown:
            raise ValueError(f"unknown ledger fields: {sorted(unknown)}")
        names = ", ".join(row)
        with closing(self._connect()) as db:
            cur = db.execute(f"INSERT INTO spend ({names}) VALUES ({', '.join('?' * len(row))})", list(row.values()))
            row["id"] = cur.lastrowid
        row["units"] = units or {}
        return row

    # ---- reading ---------------------------------------------------------

    def import_legacy(self, rows: list[dict]) -> int:
        """Insert old usage.jsonl rows in one transaction (source 'usage.jsonl')."""
        with closing(self._connect()) as db, transaction(db):
            for r in rows:
                r = {**from_legacy(r), "source": "usage.jsonl"}
                r["units"] = json.dumps(r["units"] or {}, ensure_ascii=False)
                db.execute(f"INSERT INTO spend ({', '.join(r)}) VALUES ({', '.join('?' * len(r))})", list(r.values()))
        return len(rows)

    def total(self, since: float) -> float:
        with closing(self._connect()) as db:
            return db.execute("SELECT COALESCE(SUM(credits), 0) FROM spend WHERE ts >= ?", (since,)).fetchone()[0]

    def by_service(self, since: float) -> dict[str, dict]:
        """{service: {credits, calls, unpriced}} since `since`, largest first."""
        with closing(self._connect()) as db:
            rows = db.execute("SELECT service, SUM(credits) AS credits, COUNT(*) AS calls, "
                              "SUM(list_usd IS NULL) AS unpriced FROM spend WHERE ts >= ? "
                              "GROUP BY service ORDER BY SUM(credits) DESC, service", (since,)).fetchall()
        return {r["service"]: {"credits": r["credits"], "calls": r["calls"], "unpriced": r["unpriced"]} for r in rows}

    def model_usage(self, since: float) -> tuple[int, int]:
        """(calls, tokens) of model calls since `since`."""
        with closing(self._connect()) as db:
            r = db.execute("SELECT COUNT(*), COALESCE(SUM(tokens), 0) FROM spend "
                           "WHERE service = 'model' AND ts >= ?", (since,)).fetchone()
        return r[0], r[1]

    def rows(self, since: float = 0.0, service: str | None = None, limit: int | None = None) -> list[dict]:
        """Rows since `since`, oldest first (newest `limit` of them when limited)."""
        sql, args = "SELECT * FROM spend WHERE ts >= ?", [since]  # type: list
        if service:
            sql += " AND service = ?"
            args.append(service)
        sql += " ORDER BY id DESC" + (f" LIMIT {int(limit)}" if limit else "")
        with closing(self._connect()) as db:
            out = [dict(r) for r in db.execute(sql, args).fetchall()]
        for r in out:
            r["units"] = json.loads(r["units"] or "{}")
        return out[::-1]

    def totals(self) -> dict:
        with closing(self._connect()) as db:
            r = db.execute("SELECT COUNT(*), COALESCE(SUM(list_usd), 0), COALESCE(SUM(paid_usd), 0), "
                           "COALESCE(SUM(credits), 0) FROM spend").fetchone()
        return {"rows": r[0], "list_usd": r[1], "paid_usd": r[2], "credits": r[3]}


class transaction:
    """BEGIN IMMEDIATE ... COMMIT, or ROLLBACK on error (autocommit connections)."""

    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def __enter__(self):
        self.db.execute("BEGIN IMMEDIATE")
        return self.db

    def __exit__(self, exc_type, *_):
        self.db.execute("ROLLBACK" if exc_type else "COMMIT")
        return False


def from_legacy(row: dict) -> dict:
    """A `usage.jsonl` row as ledger columns."""
    cost = row.get("cost")
    return {
        "ts": float(row["ts"]),
        "service": row.get("service") or "model",
        "provider": row.get("provider"),
        "model": row.get("model"),
        "kind": row.get("kind"),
        "thread": row.get("thread"),
        "what": row.get("what"),
        "units": row.get("usage") or ({"units": row["units"], "unit": row.get("unit")} if "units" in row else {}),
        "tokens": row.get("tokens"),
        "list_usd": cost,
        "paid_usd": None,
        "credits": row["credits"] if row.get("credits") is not None else (cost or 0.0),
    }
