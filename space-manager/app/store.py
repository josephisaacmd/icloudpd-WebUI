"""SQLite persistence: scanned asset cache, scan bookkeeping, audit log.

A fresh connection per call keeps this trivially thread-safe (scans run in a
background thread while the API serves reads).
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
    account            TEXT NOT NULL,
    master_id          TEXT NOT NULL,
    asset_record_name  TEXT NOT NULL,
    asset_change_tag   TEXT NOT NULL,
    filename           TEXT NOT NULL,
    size               INTEGER NOT NULL,
    lp_size            INTEGER,
    asset_date         TEXT NOT NULL,
    item_type          TEXT NOT NULL,
    album              TEXT NOT NULL,
    scanned_at         TEXT NOT NULL,
    PRIMARY KEY (account, master_id)
);
CREATE INDEX IF NOT EXISTS idx_assets_account_size ON assets (account, size DESC);

CREATE TABLE IF NOT EXISTS scans (
    account     TEXT PRIMARY KEY,
    album       TEXT,
    started_at  TEXT,
    finished_at TEXT,
    count       INTEGER
);

CREATE TABLE IF NOT EXISTS audit_log (
    ts        TEXT NOT NULL,
    account   TEXT NOT NULL,
    master_id TEXT NOT NULL,
    filename  TEXT NOT NULL,
    size      INTEGER NOT NULL,
    action    TEXT NOT NULL,
    result    TEXT NOT NULL
);
"""


class Store:
    def __init__(self, db_path: str):
        self.db_path = db_path
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    # -- scan lifecycle -----------------------------------------------------

    def begin_scan(self, account: str, album: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute("DELETE FROM assets WHERE account = ?", (account,))
            conn.execute(
                "INSERT INTO scans (account, album, started_at, finished_at, count) "
                "VALUES (?, ?, ?, NULL, 0) "
                "ON CONFLICT(account) DO UPDATE SET album = excluded.album, "
                "started_at = excluded.started_at, finished_at = NULL, count = 0",
                (account, album, now),
            )

    def add_assets(self, account: str, album: str, rows: Iterable[dict[str, Any]]) -> int:
        now = datetime.now(timezone.utc).isoformat()
        rows = list(rows)
        with self._conn() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO assets (account, master_id, asset_record_name, "
                "asset_change_tag, filename, size, lp_size, asset_date, item_type, album, scanned_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        account,
                        r["master_id"],
                        r["asset_record_name"],
                        r["asset_change_tag"],
                        r["filename"],
                        r["size"],
                        r.get("lp_size"),
                        r["asset_date"],
                        r["item_type"],
                        album,
                        now,
                    )
                    for r in rows
                ],
            )
            conn.execute(
                "UPDATE scans SET count = count + ? WHERE account = ?",
                (len(rows), account),
            )
        return len(rows)

    def finish_scan(self, account: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute(
                "UPDATE scans SET finished_at = ? WHERE account = ?", (now, account)
            )

    def scan_info(self, account: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM scans WHERE account = ?", (account,)
            ).fetchone()
            return dict(row) if row else None

    # -- queries ------------------------------------------------------------

    def list_assets(
        self,
        account: str,
        item_type: str | None = None,
        min_size: int | None = None,
        older_than_days: int | None = None,
        name_contains: str | None = None,
        sort: str = "size",
        limit: int = 500,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        where = ["account = ?"]
        params: list[Any] = [account]
        if item_type:
            where.append("item_type = ?")
            params.append(item_type)
        if min_size:
            where.append("size >= ?")
            params.append(min_size)
        if older_than_days is not None:
            cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
            where.append("asset_date < ?")
            params.append(cutoff.isoformat())
        if name_contains:
            where.append("filename LIKE ?")
            params.append(f"%{name_contains}%")
        order = {
            "size": "size DESC",
            "date": "asset_date ASC",
            "date_desc": "asset_date DESC",
        }.get(sort, "size DESC")
        sql = (
            f"SELECT * FROM assets WHERE {' AND '.join(where)} "
            f"ORDER BY {order} LIMIT ? OFFSET ?"
        )
        params += [limit, offset]
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def get_assets(self, account: str, master_ids: list[str]) -> list[dict[str, Any]]:
        if not master_ids:
            return []
        marks = ",".join("?" * len(master_ids))
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM assets WHERE account = ? AND master_id IN ({marks})",
                [account, *master_ids],
            ).fetchall()
            return [dict(r) for r in rows]

    def remove_assets(self, account: str, master_ids: list[str]) -> None:
        if not master_ids:
            return
        marks = ",".join("?" * len(master_ids))
        with self._conn() as conn:
            conn.execute(
                f"DELETE FROM assets WHERE account = ? AND master_id IN ({marks})",
                [account, *master_ids],
            )

    # -- audit --------------------------------------------------------------

    def audit(
        self, account: str, master_id: str, filename: str, size: int, action: str, result: str
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO audit_log (ts, account, master_id, filename, size, action, result) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    datetime.now(timezone.utc).isoformat(),
                    account,
                    master_id,
                    filename,
                    size,
                    action,
                    result,
                ),
            )

    def audit_entries(self, account: str, limit: int = 200) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM audit_log WHERE account = ? ORDER BY ts DESC LIMIT ?",
                (account, limit),
            ).fetchall()
            return [dict(r) for r in rows]
