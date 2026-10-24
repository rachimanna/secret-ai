"""Versioned, verified SQLite snapshots in Postgres; restoration fails closed."""
import asyncio
import hashlib
import logging
import os
import sqlite3
import tempfile
import threading
import time
import zlib
from pathlib import Path

LOG = logging.getLogger(__name__)
SAVE_EVERY = 60
TABLE = ("CREATE TABLE IF NOT EXISTS bot_snapshots (name TEXT PRIMARY KEY, data BYTEA NOT NULL, "
         "updated TIMESTAMPTZ NOT NULL DEFAULT now())")
VERSIONS = ("CREATE TABLE IF NOT EXISTS bot_snapshot_versions (id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL, "
            "data BYTEA NOT NULL, digest TEXT NOT NULL, updated TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE(name,digest))")


def unpack(packed):
    decoder = zlib.decompressobj()
    data = decoder.decompress(bytes(packed), 256 * 1024 * 1024 + 1)
    if len(data) > 256 * 1024 * 1024 or not decoder.eof or decoder.unused_data:
        raise ValueError("Invalid or oversized snapshot")
    verify(data)
    return data


def verify(data):
    with tempfile.TemporaryDirectory(prefix="secret-verify-") as folder:
        path = Path(folder) / "check.db"
        path.write_bytes(data)
        with sqlite3.connect(path) as db:
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Snapshot integrity check failed")


class Snapshots:
    def __init__(self, url, path, name="memory.db"):
        self.url, self.path, self.name = url, Path(path), name
        self.saved_hash = None
        self.ready = False
        self.last_success = None
        self.last_error = ""
        self.lock = threading.Lock()

    def _connect(self):
        import psycopg
        return psycopg.connect(self.url, connect_timeout=20, autocommit=True)

    def _tables(self, conn):
        conn.execute(TABLE)
        conn.execute(VERSIONS)
        conn.execute("CREATE TABLE IF NOT EXISTS bot_snapshot_restore (name TEXT PRIMARY KEY, data BYTEA NOT NULL)")

    def _install(self, data):
        verify(data)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".restoring")
        temporary.write_bytes(data)
        # Called before any SQLite connections are opened.
        for suffix in ("-wal", "-shm"):
            Path(str(self.path) + suffix).unlink(missing_ok=True)
        temporary.replace(self.path)
        self.saved_hash = hashlib.sha256(data).hexdigest()

    def restore(self):
        with self.lock:
            try:
                with self._connect() as conn:
                    self._tables(conn)
                    pending = conn.execute("SELECT data FROM bot_snapshot_restore WHERE name=%s",(self.name,)).fetchone()
                    if pending:
                        self._install(unpack(pending[0]))
                        conn.execute("DELETE FROM bot_snapshot_restore WHERE name=%s",(self.name,))
                        self.ready = True
                        return True
                    if self.path.exists() and self.path.stat().st_size > 0:
                        self.ready = True
                        return False
                    row = conn.execute("SELECT data FROM bot_snapshots WHERE name=%s", (self.name,)).fetchone()
                if row:
                    self._install(unpack(row[0]))
                self.ready = True
                self.last_error = ""
                return bool(row)
            except Exception as exc:
                self.last_error = type(exc).__name__
                # An existing local database is usable during a Postgres outage.
                if self.path.exists() and self.path.stat().st_size > 0:
                    self.ready = True
                    return False
                raise

    def _copy(self):
        with tempfile.TemporaryDirectory(prefix="secret-ai-snap-") as folder:
            target = Path(folder) / "copy.db"
            source, copy = sqlite3.connect(self.path, timeout=30), sqlite3.connect(target)
            try:
                source.backup(copy)
            finally:
                copy.close()
                source.close()
            data = target.read_bytes()
            verify(data)
            return data

    def save(self):
        with self.lock:
            if not self.ready:
                raise RuntimeError("Restore must succeed before saving")
            if not self.path.exists():
                return False
            data = self._copy()
            digest = hashlib.sha256(data).hexdigest()
            if digest == self.saved_hash:
                return False
            packed = zlib.compress(data, 6)
            try:
                with self._connect() as conn, conn.transaction():
                    self._tables(conn)
                    old = conn.execute("SELECT data,updated FROM bot_snapshots WHERE name=%s", (self.name,)).fetchone()
                    if old:
                        old_digest = hashlib.sha256(unpack(old[0])).hexdigest()
                        conn.execute("INSERT INTO bot_snapshot_versions(name,data,digest,updated) VALUES (%s,%s,%s,%s) "
                                     "ON CONFLICT(name,digest) DO NOTHING", (self.name,old[0],old_digest,old[1]))
                    conn.execute("INSERT INTO bot_snapshot_versions(name,data,digest) VALUES (%s,%s,%s) "
                                 "ON CONFLICT(name,digest) DO NOTHING", (self.name,packed,digest))
                    conn.execute("INSERT INTO bot_snapshots(name,data,updated) VALUES (%s,%s,now()) ON CONFLICT(name) "
                                 "DO UPDATE SET data=EXCLUDED.data,updated=now()", (self.name,packed))
                    conn.execute("DELETE FROM bot_snapshot_versions WHERE name=%s AND id NOT IN "
                                 "(SELECT id FROM bot_snapshot_versions WHERE name=%s ORDER BY id DESC LIMIT 24)",
                                 (self.name,self.name))
                self.saved_hash = digest
                self.last_success = int(time.time())
                self.last_error = ""
                return True
            except Exception as exc:
                self.last_error = type(exc).__name__
                raise

    def versions(self):
        with self.lock, self._connect() as conn:
            self._tables(conn)
            rows = conn.execute("SELECT id,digest,updated,octet_length(data) FROM bot_snapshot_versions "
                                "WHERE name=%s ORDER BY id DESC LIMIT 24", (self.name,)).fetchall()
        return [{"id":r[0],"digest":r[1],"created":r[2].isoformat(),"bytes":r[3]} for r in rows]

    def stage_restore(self, version):
        if type(version) is not int or version <= 0:
            raise ValueError("version")
        # Save current data before scheduling rollback. The running database is never replaced.
        self.save()
        with self.lock, self._connect() as conn:
            row = conn.execute("SELECT data FROM bot_snapshot_versions WHERE id=%s AND name=%s",
                               (version,self.name)).fetchone()
            if not row:
                raise ValueError("version not found")
            data = unpack(row[0])
            conn.execute("INSERT INTO bot_snapshot_restore(name,data) VALUES (%s,%s) ON CONFLICT(name) "
                         "DO UPDATE SET data=EXCLUDED.data",(self.name,zlib.compress(data,6)))
        return {"scheduled":True,"restart_required":True}

    async def run(self):
        while True:
            await asyncio.sleep(SAVE_EVERY)
            try:
                await asyncio.to_thread(self.save)
            except Exception as exc:
                LOG.warning("Saving database failed: %s", type(exc).__name__)


def from_env(path):
    url = os.environ.get("DATABASE_URL", "").strip()
    return Snapshots(url,path) if url else None
