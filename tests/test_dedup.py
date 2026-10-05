import sqlite3
from pathlib import Path
import pytest
from app.core.database import Database
from app.services.twitter_service import TwitterService
from app.services import twitter_service as module
from app.core import database as database_module


def tweet(i):
    return {"id": str(i), "text": "buy!", "createdAt": "2026-10-04T00:00:00Z", "author": {"userName": "alice"}}

class Client:
    pages = []
    calls = []
    def __init__(self, **kwargs): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def get(self, *args, **kwargs):
        self.calls.append(kwargs.get("params"))
        data = self.pages.pop(0)
        if isinstance(data, Exception): raise data
        class Response:
            def raise_for_status(self): pass
            def json(self): return data
        return Response()

@pytest.fixture
def provider(monkeypatch):
    Client.pages = []
    Client.calls = []
    monkeypatch.setattr(module.httpx, "AsyncClient", Client)
    return Client

@pytest.mark.asyncio
async def test_50_100_500(provider):
    service = TwitterService("synthetic")
    for count in (50, 100, 500):
        provider.pages = [{"tweets": [tweet(i) for i in range(start, start+30)], "next_cursor": str(start+25), "has_next_page": True} for start in range(0, count, 25)]
        result = await service.fetch_tweets("BTC", count)
        assert result["count"] == len({t["id"] for t in result["tweets"]}) == count
        assert result["status"] == "ok"
    assert len(await module.db.get_known_ids("BTC")) == 500

@pytest.mark.asyncio
@pytest.mark.parametrize("second,reason", [
    ({"tweets": [tweet(1)], "next_cursor": "a", "has_next_page": True}, "pagination_no_progress"),
    ({"tweets": [tweet(2)], "next_cursor": "a", "has_next_page": True}, "pagination_no_progress"),
    ({"tweets": [tweet(2)], "has_next_page": True}, "pagination_no_progress"),
    ({"tweets": [], "has_next_page": False}, "end_of_results")])
async def test_pagination_no_progress(provider, second, reason):
    provider.pages = [{"tweets": [tweet(1), tweet(1), {"id": ""}, {"id": None}], "next_cursor": "a"}, second]
    res = await TwitterService("synthetic").fetch_tweets("BTC", 50)
    assert res["status"] == "partial" and res["reason"] == reason
    assert len({t["id"] for t in res["tweets"]}) == res["count"] <= 2
    assert all(isinstance(c.get("cursor", ""), str) for c in provider.calls)

@pytest.mark.asyncio
async def test_oversized_page(provider):
    provider.pages = [{"tweets": [tweet(i) for i in range(80)], "has_next_page": False}]
    res = await TwitterService("synthetic").fetch_tweets("BTC", 50)
    assert res["count"] == 50

@pytest.mark.asyncio
async def test_no_demo_or_old_cache(provider):
    service = TwitterService()
    res = await service.fetch_tweets("BTC", 50)
    assert res["tweets"] == [] and res["status"] == "unavailable"
    await module.db.save_tweets("BTC", [{"id": "demo", "text": "BUY"}], source="demo")
    service.api_key = "synthetic"
    provider.pages = [{"tweets": [], "has_next_page": False}]
    res = await service.fetch_tweets("BTC", 50)
    assert res["tweets"] == [] and res["status"] == "unavailable"
    assert await module.db.get_recent_tweets("BTC") == []

@pytest.mark.asyncio
async def test_relations_and_update(tmp_path):
    db = Database(tmp_path / "test.db")
    assert await db.save_tweets("BTC", [{"id": "same", "likes": 1}]) == 1
    assert await db.save_tweets("ETH", [{"id": "same", "likes": 2}]) == 1
    assert await db.save_tweets("ETH", [{"id": "same", "likes": 3}]) == 0
    for symbol in ("BTC", "ETH"):
        rows = await db.get_recent_tweets(symbol)
        assert len(rows) == 1 and rows[0]["likes"] == 3

@pytest.mark.asyncio
async def test_legacy_quarantine_backup(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE tweets(id TEXT PRIMARY KEY,symbol TEXT,text TEXT)")
        conn.execute("INSERT INTO tweets VALUES('demo','BTC','BUY')")
    db = Database(path)
    assert await db.get_recent_tweets("BTC") == []
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM tweets").fetchone()[0] == 1
    with sqlite3.connect(str(path) + ".legacy-backup") as conn:
        assert conn.execute("SELECT text FROM tweets").fetchone()[0] == "BUY"

@pytest.mark.asyncio
async def test_invalid_symbol_rejected():
    with pytest.raises(ValueError):
        await TwitterService().fetch_tweets("BTC;DROP", 50)


def make_legacy(path):
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE tweets(id TEXT PRIMARY KEY,symbol TEXT,text TEXT)")
        conn.execute("INSERT INTO tweets VALUES('legacy','BTC','preserve me')")


@pytest.mark.parametrize("stage", ["copy", "fsync", "replace"])
def test_backup_failure_then_retry(tmp_path, monkeypatch, stage):
    path = tmp_path / "legacy.db"
    make_legacy(path)
    backup = Path(str(path) + ".legacy-backup")
    real_connect = sqlite3.connect
    failure = OSError("synthetic backup interruption")
    class Interrupted(sqlite3.Connection):
        def backup(self, *args, **kwargs):
            raise failure
    def fail(*args, **kwargs):
        raise failure
    db = Database(path)
    with monkeypatch.context() as patch:
        if stage == "copy":
            patch.setattr(sqlite3, "connect", lambda *a, **k: real_connect(*a, **k, factory=Interrupted))
        else:
            patch.setattr(database_module.os, stage, fail)
        with pytest.raises(OSError) as exc:
            db.init_db()
        assert exc.value is failure
    assert not backup.exists()
    assert not list(tmp_path.glob(".legacy-backup-*.tmp"))
    with real_connect(path) as conn:
        assert conn.execute("SELECT text FROM tweets").fetchone()[0] == "preserve me"
        assert not conn.execute("SELECT name FROM sqlite_master WHERE name='tweet_records'").fetchall()
    db.init_db()
    with real_connect(backup) as conn:
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT text FROM tweets").fetchone()[0] == "preserve me"
    assert backup.stat().st_mode & 0o777 == 0o600
    saved = backup.read_bytes()
    Database(path).init_db()
    assert backup.read_bytes() == saved


@pytest.mark.parametrize("kind", ["empty", "corrupt", "missing_rows", "wrong_schema"])
@pytest.mark.parametrize("already_v2", [False, True])
def test_invalid_existing_backup_rebuilt(tmp_path, kind, already_v2):
    path = tmp_path / "legacy.db"
    make_legacy(path)
    backup = Path(str(path) + ".legacy-backup")
    if kind in ("empty", "corrupt"):
        backup.write_bytes(b"" if kind == "empty" else b"not a database")
    else:
        with sqlite3.connect(backup) as conn:
            if kind == "missing_rows":
                conn.execute("CREATE TABLE tweets(id TEXT PRIMARY KEY,symbol TEXT,text TEXT)")
            else:
                conn.execute("CREATE TABLE tweets(id TEXT)")
                conn.execute("INSERT INTO tweets VALUES('legacy')")
    if already_v2:
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE tweet_records(source TEXT,id TEXT,payload TEXT,fetched_at REAL,PRIMARY KEY(source,id))")
    Database(path).init_db()
    with sqlite3.connect(backup) as conn:
        assert conn.execute("SELECT text FROM tweets").fetchone()[0] == "preserve me"
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT text FROM tweets").fetchone()[0] == "preserve me"


def test_incomplete_copy_never_published(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    make_legacy(path)
    real_connect = sqlite3.connect
    class Incomplete(sqlite3.Connection):
        def backup(self, destination, **kwargs):
            destination.execute("CREATE TABLE tweets(id TEXT PRIMARY KEY,symbol TEXT,text TEXT)")
            destination.commit()  # Valid SQLite and matching schema, but missing the source row.
    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", lambda *a, **k: real_connect(*a, **k, factory=Incomplete))
        with pytest.raises(sqlite3.DatabaseError, match="Incomplete legacy backup"):
            Database(path).init_db()
    assert not Path(str(path) + ".legacy-backup").exists()
    assert not list(tmp_path.glob(".legacy-backup-*.tmp"))
    with real_connect(path) as conn:
        assert not conn.execute("SELECT name FROM sqlite_master WHERE name='tweet_records'").fetchall()
