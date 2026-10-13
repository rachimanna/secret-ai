"""Keep the SQLite database across Render deploys: a compressed snapshot lives in Postgres (e.g. Neon, free).

On start, before the database is opened, the latest snapshot is written to disk if the file isn't there (a fresh
deploy). While the bot runs, a consistent copy (SQLite's backup API, safe with WAL) is uploaded whenever it changed,
at most once a minute, and once more on shutdown. Set DATABASE_URL to the Postgres connection string to turn it on.
"""
import asyncio
import hashlib
import logging
import os
import sqlite3
import tempfile
import zlib
from pathlib import Path

LOG = logging.getLogger(__name__)
SAVE_EVERY = 60  # seconds
TABLE = ("CREATE TABLE IF NOT EXISTS bot_snapshots (name TEXT PRIMARY KEY, data BYTEA NOT NULL, "
         "updated TIMESTAMPTZ NOT NULL DEFAULT now())")


class Snapshots:
    def __init__(self, url, path, name="memory.db"):
        self.url, self.path, self.name = url, Path(path), name
        self.saved_hash = None

    def _connect(self):
        import psycopg  # only needed when DATABASE_URL is set

        return psycopg.connect(self.url, connect_timeout=20, autocommit=True)

    def restore(self):
        """Write the stored snapshot to disk if the database file is missing. True if restored."""
        if self.path.exists() and self.path.stat().st_size > 0:
            return False
        with self._connect() as conn:
            conn.execute(TABLE)
            row = conn.execute("SELECT data FROM bot_snapshots WHERE name = %s", (self.name,)).fetchone()
        if not row:
            LOG.info("No saved database in Postgres yet; starting fresh")
            return False
        data = zlib.decompress(bytes(row[0]))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".restoring")
        temporary.write_bytes(data)
        temporary.replace(self.path)
        self.saved_hash = hashlib.sha256(data).hexdigest()
        LOG.info("Database restored from Postgres (%d KB)", len(data) // 1024)
        return True

    def _copy(self):
        """A consistent copy of the live database as bytes."""
        with tempfile.TemporaryDirectory(prefix="secret-ai-snap-") as folder:
            target = Path(folder) / "copy.db"
            source, copy = sqlite3.connect(self.path, timeout=30), sqlite3.connect(target)
            try:
                source.backup(copy)
            finally:
                copy.close()
                source.close()
            return target.read_bytes()

    def save(self):
        """Upload the database if it changed since the last upload. True if uploaded."""
        if not self.path.exists():
            return False
        data = self._copy()
        digest = hashlib.sha256(data).hexdigest()
        if digest == self.saved_hash:
            return False
        packed = zlib.compress(data, 6)
        with self._connect() as conn:
            conn.execute(TABLE)
            conn.execute("INSERT INTO bot_snapshots(name, data, updated) VALUES (%s, %s, now()) ON CONFLICT (name) "
                         "DO UPDATE SET data = EXCLUDED.data, updated = now()", (self.name, packed))
        self.saved_hash = digest
        return True

    async def run(self):
        """Save every SAVE_EVERY seconds while the bot runs."""
        while True:
            await asyncio.sleep(SAVE_EVERY)
            try:
                await asyncio.to_thread(self.save)
            except Exception as exc:  # the next round tries again; the bot keeps working meanwhile
                LOG.warning("Saving the database to Postgres failed: %s", type(exc).__name__)


def from_env(path):
    url = os.environ.get("DATABASE_URL", "").strip()
    return Snapshots(url, path) if url else None
