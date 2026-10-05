"""Source-aware storage. Legacy tweets remain quarantined, never read as live."""
from contextlib import contextmanager, closing, suppress
from collections import Counter
import asyncio
import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path

DB_PATH = Path(os.environ.get("JEV_DB_PATH", Path(__file__).resolve().parent.parent / "data" / "market_intel.db"))


class Database:
    def __init__(self, path=None):
        self.path = Path(path or DB_PATH)
        self._backup_verified = False

    @staticmethod
    def _valid_legacy_backup(path, source):
        """An existing filename alone is not a completed SQLite snapshot."""
        try:
            if not path.is_file() or path.stat().st_size == 0:
                return False
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as backup:
                if backup.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    return False
                schema = backup.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='tweets'").fetchone()
                expected = source.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='tweets'").fetchone()
                if schema is None or expected is None or schema[0] != expected[0]:
                    return False
                # Compare every legacy value and its SQLite storage type, including
                # duplicate multiplicity; row insertion order is not significant.
                def contents(connection):
                    return Counter(
                        tuple((type(value).__name__, value) for value in row)
                        for row in connection.execute("SELECT * FROM tweets")
                    )
                return contents(backup) == contents(source)
        except (OSError, sqlite3.Error):
            return False

    def _backup_legacy(self, source, target):
        # Private temporary file in the same directory; publish only after validation.
        fd, temporary = tempfile.mkstemp(prefix=".legacy-backup-", suffix=".tmp", dir=target.parent)
        os.close(fd)
        try:
            with closing(sqlite3.connect(temporary)) as destination:
                source.backup(destination)
            if not self._valid_legacy_backup(Path(temporary), source):
                raise sqlite3.DatabaseError("Incomplete legacy backup")
            with open(temporary, "rb") as stream:
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            with suppress(OSError):
                os.unlink(temporary)

    @contextmanager
    def _get_connection(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def init_db(self):
        with self._get_connection() as conn:
            # Also repair incomplete backups left by the former migration, even with v2 present.
            legacy = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tweets'").fetchone()
            if legacy and not self._backup_verified:
                backup = self.path.with_name(self.path.name + ".legacy-backup")
                if not self._valid_legacy_backup(backup, conn):
                    self._backup_legacy(conn, backup)
                self._backup_verified = True
            conn.execute("CREATE TABLE IF NOT EXISTS tweet_records (source TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL, fetched_at REAL NOT NULL, PRIMARY KEY(source,id))")
            conn.execute("CREATE TABLE IF NOT EXISTS tweet_symbols (source TEXT NOT NULL, id TEXT NOT NULL, symbol TEXT NOT NULL, PRIMARY KEY(source,id,symbol))")

    async def save_tweets(self, symbol, tweets, source="twitterapi.io"):
        def save():
            self.init_db()
            with self._get_connection() as conn:
                count = 0
                for tweet in tweets:
                    tid = tweet.get("id")
                    if not isinstance(tid, str) or not tid.strip():
                        continue
                    record = dict(tweet, source=source)
                    conn.execute("INSERT INTO tweet_records VALUES(?,?,?,?) ON CONFLICT(source,id) DO UPDATE SET payload=excluded.payload,fetched_at=excluded.fetched_at", (source, tid, json.dumps(record), time.time()))
                    count += conn.execute("INSERT OR IGNORE INTO tweet_symbols VALUES(?,?,?)", (source, tid, symbol.upper())).rowcount
                return count
        return await asyncio.to_thread(save)

    async def get_recent_tweets(self, symbol, limit=100, source="twitterapi.io"):
        def query():
            self.init_db()
            with self._get_connection() as conn:
                rows = conn.execute("SELECT r.payload FROM tweet_records r JOIN tweet_symbols s ON r.source=s.source AND r.id=s.id WHERE s.symbol=? AND s.source=? ORDER BY r.fetched_at DESC,r.id DESC LIMIT ?", (symbol.upper(), source, limit)).fetchall()
                return [json.loads(row["payload"]) for row in rows]
        return await asyncio.to_thread(query)

    async def get_known_ids(self, symbol, limit=5000):
        return {t["id"] for t in await self.get_recent_tweets(symbol, limit)}


# Importing the application must not create or migrate a database.
db = Database()
