"""Synthetic legacy backups: structural SQLite health is not content equality."""
import sqlite3
from pathlib import Path

import pytest
from app.core.database import Database
from app.core import database as module

SCHEMA = "CREATE TABLE tweets(id TEXT PRIMARY KEY,symbol TEXT,text TEXT)"
ROWS = [("1", "BTC", "original payload żółć"), ("2", "SOL", "second payload")]


def create(path, rows=ROWS, schema=SCHEMA):
    with sqlite3.connect(path) as conn:
        conn.execute(schema)
        conn.executemany("INSERT INTO tweets VALUES(?,?,?)", rows)


def rows(path):
    with sqlite3.connect(path) as conn:
        return conn.execute("SELECT * FROM tweets ORDER BY id").fetchall()


@pytest.mark.parametrize("column", [0, 1, 2])
@pytest.mark.parametrize("already_v2", [False, True])
def test_same_count_changed_backup_repaired(tmp_path, column, already_v2):
    source = tmp_path / "source.db"
    backup = Path(str(source) + ".legacy-backup")
    create(source)
    changed = [list(row) for row in ROWS]
    changed[0][column] = "changed content"
    create(backup, changed)
    if already_v2:
        with sqlite3.connect(source) as conn:
            conn.execute("CREATE TABLE tweet_records(source TEXT,id TEXT,payload TEXT,fetched_at REAL,PRIMARY KEY(source,id))")
    with sqlite3.connect(source) as conn:
        assert not Database._valid_legacy_backup(backup, conn)
    Database(source).init_db()
    assert rows(source) == ROWS
    assert rows(backup) == ROWS


def test_valid_backup_different_insertion_order_preserved(tmp_path, monkeypatch):
    source = tmp_path / "source.db"
    backup = Path(str(source) + ".legacy-backup")
    create(source)
    create(backup, list(reversed(ROWS)))
    before = backup.read_bytes(), backup.stat().st_mtime_ns, backup.stat().st_ino
    def unnecessary_copy(*args):
        pytest.fail("Valid legacy backup must not be rewritten")
    monkeypatch.setattr(Database, "_backup_legacy", unnecessary_copy)
    Database(source).init_db()
    assert (backup.read_bytes(), backup.stat().st_mtime_ns, backup.stat().st_ino) == before
    assert rows(source) == ROWS


@pytest.mark.parametrize("bad_rows", [
    [(None, b"one", 1), (None, b"one", 1)],  # Changed multiplicity, same count.
    [(None, b"one", 1.0), (None, b"two", 2)],  # Changed SQLite storage type.
    [(None, b"changed", 1), (None, b"two", 2)],
])
def test_all_values_types_and_multiplicity_checked(tmp_path, bad_rows):
    schema = "CREATE TABLE tweets(id,symbol,text)"
    original = [(None, b"one", 1), (None, b"two", 2)]
    source, backup = tmp_path / "source.db", tmp_path / "backup.db"
    create(source, original, schema)
    create(backup, bad_rows, schema)
    with sqlite3.connect(source) as conn:
        assert not Database._valid_legacy_backup(backup, conn)


def test_bad_backup_replacement_failure_then_fresh_retry(tmp_path, monkeypatch):
    source = tmp_path / "source.db"
    backup = Path(str(source) + ".legacy-backup")
    create(source)
    create(backup, [("1", "BTC", "WRONG"), ROWS[1]])
    before = backup.read_bytes()
    def fail(*args):
        raise OSError("controlled replacement failure")
    with monkeypatch.context() as patch:
        patch.setattr(module.os, "replace", fail)
        with pytest.raises(OSError, match="controlled replacement failure"):
            Database(source).init_db()
    assert backup.read_bytes() == before
    assert rows(source) == ROWS
    assert not list(tmp_path.glob(".legacy-backup-*.tmp"))
    with sqlite3.connect(source) as conn:
        assert not conn.execute("SELECT name FROM sqlite_master WHERE name='tweet_records'").fetchall()
    Database(source).init_db()
    assert rows(backup) == rows(source) == ROWS
    assert backup.stat().st_mode & 0o777 == 0o600
