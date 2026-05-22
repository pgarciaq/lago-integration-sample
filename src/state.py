"""SQLite-based state tracking for sync operations."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

DEFAULT_DB_PATH = Path.home() / ".lago-sync" / "state.db"


class SyncState:
    """Tracks which (customer, provider, date) combinations have been synced."""

    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._init_schema()

    def _init_schema(self):
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS sync_log (
                org_id TEXT NOT NULL,
                customer_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                sync_date TEXT NOT NULL,
                synced_at TEXT NOT NULL,
                event_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (org_id, customer_id, provider, sync_date)
            )
        """)
        self._conn.commit()

    def is_synced(self, org_id: str, customer_id: str, provider: str, sync_date: date) -> bool:
        """Check if a given combination has already been synced."""
        cursor = self._conn.execute(
            "SELECT 1 FROM sync_log WHERE org_id=? AND customer_id=? AND provider=? AND sync_date=?",
            (org_id, customer_id, provider, sync_date.isoformat()),
        )
        return cursor.fetchone() is not None

    def mark_synced(
        self, org_id: str, customer_id: str, provider: str, sync_date: date, event_count: int
    ):
        """Record a successful sync."""
        now = datetime.now(tz=timezone.utc).isoformat()
        self._conn.execute(
            """INSERT OR REPLACE INTO sync_log (org_id, customer_id, provider, sync_date, synced_at, event_count)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (org_id, customer_id, provider, sync_date.isoformat(), now, event_count),
        )
        self._conn.commit()

    def get_sync_summary(self, org_id: str, month: str) -> list[dict]:
        """Get sync records for a given month (YYYY-MM format)."""
        cursor = self._conn.execute(
            "SELECT customer_id, provider, sync_date, synced_at, event_count FROM sync_log "
            "WHERE org_id=? AND sync_date LIKE ?",
            (org_id, f"{month}%"),
        )
        return [
            {
                "customer_id": row[0],
                "provider": row[1],
                "sync_date": row[2],
                "synced_at": row[3],
                "event_count": row[4],
            }
            for row in cursor.fetchall()
        ]

    def close(self):
        self._conn.close()
