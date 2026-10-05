"""Offline by construction, including collection/import time."""
import os
import builtins
import io
import socket
import tempfile
from pathlib import Path
import sys
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SANDBOX = Path(tempfile.mkdtemp(prefix="jev-tests-"))
os.environ["JEV_CONFIG_FILE"] = str(SANDBOX / "settings.test")
os.environ["JEV_DB_PATH"] = str(SANDBOX / "test.sqlite")
os.environ["TYPESAFE_API_KEY"] = ""
os.environ["TWITTER_API_KEY"] = ""
os.environ["ADMIN_TOKEN"] = ""

# Fail closed if a future import tries to read a real .env file.
_original_open = builtins.open
_original_io_open = io.open

def guarded_open(original):
    def open_file(file, *args, **kwargs):
        if isinstance(file, (str, bytes, os.PathLike)) and Path(os.fsdecode(file)).name == ".env":
            raise AssertionError("Offline suite forbids .env access")
        return original(file, *args, **kwargs)
    return open_file

builtins.open = guarded_open(_original_open)
io.open = guarded_open(_original_io_open)

def denied(*args, **kwargs):
    raise AssertionError("Offline suite forbids network")

# Installed before application imports and retained across fixture teardown.
socket.socket.connect = denied
socket.socket.connect_ex = denied
socket.create_connection = denied
socket.getaddrinfo = denied

@pytest.fixture(autouse=True)
def isolation(monkeypatch, tmp_path):
    from app.core.config import settings
    from app.core.database import Database
    from app.core.cache import social_cache, market_cache
    from app.services import twitter_service as twitter_module
    from app.services.typesafe_service import typesafe_service
    from app.api.v1 import analyze
    monkeypatch.setenv("JEV_CONFIG_FILE", str(tmp_path / "settings.test"))
    monkeypatch.setattr(analyze, "CONFIG_PATH", tmp_path / "settings.test")
    monkeypatch.setattr(twitter_module, "db", Database(tmp_path / "test.sqlite"))
    monkeypatch.setattr(settings, "TWITTER_API_KEY", None)
    monkeypatch.setattr(settings, "TYPESAFE_API_KEY", None)
    monkeypatch.setattr(twitter_module.twitter_service, "api_key", None)
    monkeypatch.setattr(typesafe_service, "api_key", None)
    social_cache._cache.clear()
    market_cache._cache.clear()
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
