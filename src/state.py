"""SQLite-based state tracking for sync operations."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

DEFAULT_DB_PATH = Path.home() / ".lago-sync" / "state.db"


def _resolve_db_path(config_path: str | None = None) -> Path:
    """Resolve state DB path from config, env var, or default."""
    if config_path:
        return Path(config_path)
    env_path = os.environ.get("LAGO_SYNC_STATE_DB")
    if env_path:
        return Path(env_path)
    return DEFAULT_DB_PATH


class SyncState:
    """Tracks which (customer, provider, date) combinations have been synced.

    Also stores per-event cost fingerprints to detect when Cost Management
    reprocesses data and amounts change.
    """

    def __init__(self, db_path: Path | str | None = None):
        resolved = _resolve_db_path(str(db_path) if db_path else None)
        self.db_path = resolved
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._init_schema()

    def _init_schema(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS sync_log (
                org_id TEXT NOT NULL,
                customer_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                sync_date TEXT NOT NULL,
                synced_at TEXT NOT NULL,
                event_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (org_id, customer_id, provider, sync_date)
            );

            CREATE TABLE IF NOT EXISTS event_costs (
                transaction_id TEXT PRIMARY KEY,
                cost_amount TEXT NOT NULL,
                cost_hash TEXT NOT NULL,
                synced_at TEXT NOT NULL
            );
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

    def store_event_cost(self, transaction_id: str, cost_amount: str, properties: dict):
        """Store the cost fingerprint for an event to detect future changes."""
        cost_hash = self._hash_properties(properties)
        now = datetime.now(tz=timezone.utc).isoformat()
        self._conn.execute(
            """INSERT OR REPLACE INTO event_costs (transaction_id, cost_amount, cost_hash, synced_at)
               VALUES (?, ?, ?, ?)""",
            (transaction_id, cost_amount, cost_hash, now),
        )

    def store_event_costs_batch(self, events: list[tuple[str, str, dict]]):
        """Batch store event costs. Each tuple is (transaction_id, cost_amount, properties)."""
        now = datetime.now(tz=timezone.utc).isoformat()
        rows = [
            (txn_id, cost_amt, self._hash_properties(props), now)
            for txn_id, cost_amt, props in events
        ]
        self._conn.executemany(
            """INSERT OR REPLACE INTO event_costs (transaction_id, cost_amount, cost_hash, synced_at)
               VALUES (?, ?, ?, ?)""",
            rows,
        )
        self._conn.commit()

    def find_changed_events(self, events: list[tuple[str, str, dict]]) -> list[tuple[str, str, str]]:
        """Compare new event costs against stored values.

        Returns list of (transaction_id, old_cost, new_cost) for events whose
        cost_amount has changed since last sync.
        """
        changed = []
        for txn_id, new_cost, new_props in events:
            cursor = self._conn.execute(
                "SELECT cost_amount, cost_hash FROM event_costs WHERE transaction_id=?",
                (txn_id,),
            )
            row = cursor.fetchone()
            if row is None:
                continue
            old_cost, old_hash = row
            new_hash = self._hash_properties(new_props)
            if old_hash != new_hash:
                changed.append((txn_id, old_cost, new_cost))
        return changed

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

    @staticmethod
    def _hash_properties(properties: dict) -> str:
        """Create a stable hash of event properties for change detection."""
        stable = json.dumps(properties, sort_keys=True)
        return hashlib.sha256(stable.encode()).hexdigest()[:16]

    def close(self):
        self._conn.close()
