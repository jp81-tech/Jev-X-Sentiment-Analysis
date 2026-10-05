import asyncio
import math
import os
import time
from pathlib import Path
from types import SimpleNamespace as NS
import pytest
import httpx
from app.main import app
from app.services import market_service as market_module, typesafe_service as model_module
from app.services.market_service import calculate_rsi
from app.services.stats_service import StatsService
from app.services.typesafe_service import TypeSafeService, ACTIONS
from app.api.v1 import analyze as api


def market(price=100, tick=.001):
    now = time.time()
    return {"symbol": "BTC", "status": "ok", "source": "kraken", "is_fallback": False,
            "fetched_at": now, "request_started_at": now, "freshness_basis": "source_timestamp", "source_timestamp": now, "valid_until": now+120, "price": price, "tick_size": tick, "rsi_14": 50, "change_24h_pct": 0}

def social():
    return {"status": "ok", "source": "twitterapi.io", "fetched_at": time.time(), "valid_until": time.time()+600, "tweets": [{"id": str(i), "text": "buy!", "source":"twitterapi.io"} for i in range(50)], "target_count": 50}

def answers(probs=True):
    return NS(answers={"trade_action": NS(choice="BUY", confidence=.42, probabilities=dict(zip(ACTIONS, [0,.61,.35,0,.04,0])) if probs else {}),
                       "sentiment_spectrum": NS(score=2), "is_short_squeeze_risk": NS(noul=.1), "catalyst_impact": NS(score=0)})

class ModelClient:
    response = None
    calls = 0
    def __init__(self, **kwargs): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def system_one(self, **kwargs):
        type(self).calls += 1
        if isinstance(self.response, Exception): raise self.response
        return self.response

@pytest.fixture
def model(monkeypatch):
    ModelClient.response = answers()
    ModelClient.calls = 0
    monkeypatch.setattr(model_module, "AsyncTypeSafeClient", ModelClient)
    return ModelClient

@pytest.mark.asyncio
async def test_provider_confidence_separate(model):
    result = await TypeSafeService("synthetic").evaluate_decision("BTC", market(), {"sample_size": 50})
    assert result["confidence_pct"] == 42
    assert result["selected_action_probability_pct"] == 61
    model.response = answers(False)
    result = await TypeSafeService("synthetic").evaluate_decision("BTC", market(), {"sample_size": 50})
    assert result["confidence_pct"] == 42 and result["action_probabilities"] is None

@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [TimeoutError(), NS(answers={})])
async def test_model_failure_no_fallback(model, failure):
    model.response = failure
    assert await TypeSafeService("synthetic").evaluate_decision("BTC", market(), {"sample_size": 50}) is None

@pytest.mark.asyncio
async def test_missing_key_and_fallback_no_model(model):
    assert await TypeSafeService().evaluate_decision("BTC", market(), {"sample_size": 50}) is None
    for change in ({"is_fallback": True}, {"status": "unavailable"}, {"valid_until": 1}, {"price": 0}, {"price": float("nan")}, {"price": float("inf")}):
        assert await TypeSafeService("synthetic").evaluate_decision("BTC", dict(market(), **change), {"sample_size": 50}) is None
    assert model.calls == 0

@pytest.mark.asyncio
@pytest.mark.parametrize("prob", [{"BUY": .61}, dict.fromkeys(ACTIONS, .5), dict.fromkeys(ACTIONS, float("nan")), dict.fromkeys(ACTIONS, float("inf"))])
async def test_invalid_distribution_rejected(model, prob):
    model.response.answers["trade_action"].probabilities = prob
    assert await TypeSafeService("synthetic").evaluate_decision("BTC", market(), {"sample_size": 50}) is None

@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["BUY", "SELL", "HOLD"])
@pytest.mark.parametrize("price,tick", [(.003,.000001), (.00001,.000000001), (100,.01)])
async def test_small_price_levels(model, action, price, tick):
    model.response.answers["trade_action"].choice = action
    res = await TypeSafeService("synthetic").evaluate_decision("BTC", market(price,tick), {"sample_size": 50})
    assert res["action"] == action
    lv = res["trade_levels"]
    if action == "HOLD":
        assert lv is None
        return
    lo, hi = lv["entry_range"]
    if action == "BUY": assert 0 < lv["stop_loss"] < lo <= price <= hi < lv["target_1"] < lv["target_2"]
    else: assert 0 < lv["target_2"] < lv["target_1"] < lo <= price <= hi < lv["stop_loss"]
    for value in (lo,hi,lv["stop_loss"],lv["target_1"],lv["target_2"]):
        assert math.isclose(value/tick, round(value/tick), abs_tol=1e-7)

@pytest.mark.asyncio
async def test_tick_collapse_rejected(model):
    assert await TypeSafeService("synthetic").evaluate_decision("BTC", market(.003,.01), {"sample_size": 50}) is None

@pytest.mark.parametrize("prices,expected", [([1]*48,50), (list(range(1,49)),100), (list(range(48,0,-1)),0), ([1,2,3],None)])
def test_rsi(prices, expected): assert calculate_rsi(prices) == expected

@pytest.mark.parametrize("text,expected", [("buy",1),("buy!",1),("(buy)",1),("sell,",-1),("#bullish!",1),("$buy",1),("buyback seller",0)])
def test_tokenization(text, expected):
    assert StatsService.process_tweets([{"id":"x","text":text}])["polarity_score"] == expected

class Exchange:
    precisionMode = market_module.ccxt.TICK_SIZE
    failure = None
    last = .00001
    live_close = 99999
    def __init__(self, *args): pass
    async def fetch_ticker(self, pair):
        if self.failure in ("ticker", "unsupported"): raise ValueError("offline provider failure")
        return {"last":self.last,"timestamp":time.time()*1000}
    def market(self, pair): return {"precision":{"price":.000000001}}
    async def fetch_ohlcv(self,*args,**kwargs):
        if self.failure == "candles": raise ValueError("offline candle failure")
        hour = int(time.time()//3600)*3600
        return [[(hour-i*3600)*1000,1,1,1,1 if i else self.live_close,1] for i in range(48)]
    async def fetch_funding_rate(self,*args): return {}
    async def close(self): pass

@pytest.fixture
def exchange(monkeypatch):
    Exchange.failure = None
    Exchange.last = .00001
    monkeypatch.setattr(market_module.ccxt, "kraken", Exchange)
    monkeypatch.setattr(market_module.ccxt, "krakenfutures", Exchange)
    return Exchange

@pytest.mark.asyncio
async def test_market_precision_closed_candles(exchange):
    result = await market_module.MarketService().get_market_data("BTC")
    assert result["price"] == .00001 and result["rsi_14"] == 50 and len(result["candles"]) == 47
    assert result["tick_size"] == .000000001

@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["ticker","candles","unsupported"])
async def test_market_failures_no_decision(exchange, model, monkeypatch, failure):
    exchange.failure = failure
    async def fetch(*a,**k): return social()
    monkeypatch.setattr(api.twitter_service,"fetch_tweets",fetch)
    monkeypatch.setattr(api.typesafe_service,"api_key","synthetic")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        result = (await client.post("/api/v1/analyze",json={"symbol":"BTC","sample_size":50})).json()
    assert result["decision"] is None and result["status"] != "success" and model.calls == 0
    assert result["market"]["price"] is None

@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"status":"partial"},{"is_mock":True},{"valid_until":1},{"source":"demo"}])
async def test_social_gate_no_model(model,monkeypatch,change):
    async def fetch(*a,**k): return dict(social(),**change)
    async def get(*a,**k): return market()
    monkeypatch.setattr(api.twitter_service,"fetch_tweets",fetch)
    monkeypatch.setattr(api.market_service,"get_market_data",get)
    result = await api.analyze_asset(api.AnalyzeRequest(symbol="BTC",sample_size=50))
    assert result["decision"] is None and model.calls == 0

@pytest.mark.asyncio
async def test_settings_invalid_and_write_failure(monkeypatch,tmp_path):
    config = Path(os.environ["JEV_CONFIG_FILE"])
    config.write_text("# retained\nUNRELATED=value\n")
    old = config.read_bytes()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        response = await client.post("/api/v1/settings", json={"typesafe_api_key":"synthetic123", "twitter_api_key":"bad key"})
        assert response.status_code == 400
        assert api.typesafe_service.api_key is None and config.read_bytes() == old
        def failed(*args): raise PermissionError("synthetic")
        monkeypatch.setattr(api.os,"replace",failed)
        response = await client.post("/api/v1/settings",json={"typesafe_api_key":"synthetic123"})
        assert response.status_code == 500
        assert api.settings.TYPESAFE_API_KEY is None and api.typesafe_service.api_key is None and config.read_bytes() == old

@pytest.mark.asyncio
async def test_settings_concurrent_atomic():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        results = await asyncio.gather(*[client.post("/api/v1/settings",json={"typesafe_api_key":f"synthetic{i}","twitter_api_key":f"twitterkey{i}"}) for i in range(5)])
    assert all(r.status_code == 200 for r in results)
    config = Path(os.environ["JEV_CONFIG_FILE"])
    content = config.read_text()
    assert f"TYPESAFE_API_KEY={api.settings.TYPESAFE_API_KEY}" in content
    assert f"TWITTER_API_KEY={api.settings.TWITTER_API_KEY}" in content
    assert api.typesafe_service.api_key == api.settings.TYPESAFE_API_KEY
    assert api.twitter_service.api_key == api.settings.TWITTER_API_KEY
    assert config.stat().st_mode & 0o777 == 0o600

@pytest.mark.asyncio
async def test_success_endpoint(model,monkeypatch):
    async def fetch(*a,**k): return social()
    async def get(*a,**k): return market()
    monkeypatch.setattr(api.twitter_service,"fetch_tweets",fetch)
    monkeypatch.setattr(api.market_service,"get_market_data",get)
    monkeypatch.setattr(api.typesafe_service,"api_key","synthetic")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        res = await client.post("/api/v1/analyze",json={"symbol":"BTC","sample_size":50})
        assert res.status_code == 200 and res.json()["decision"]["confidence_pct"] == 42
        assert (await client.post("/api/v1/analyze",json={"symbol":"BTC;","sample_size":50})).status_code == 400


def test_import_did_not_create_database():
    from conftest import SANDBOX
    assert not (SANDBOX/"test.sqlite").exists()


def test_network_guard():
    import socket
    with pytest.raises(AssertionError,match="forbids network"):
        socket.create_connection(("example.invalid",443))


def test_ui_node():
    import subprocess
    result = subprocess.run(["node", "tests/test_ui.js"],capture_output=True,text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "27 UI checks passed" in result.stdout

@pytest.mark.asyncio
@pytest.mark.parametrize("price", [0, float("nan"), float("inf")])
async def test_market_invalid_price(exchange,price):
    exchange.last = price
    result = await market_module.MarketService().get_market_data("BTC")
    assert result["status"] == "unavailable" and result["price"] is None

@pytest.mark.asyncio
@pytest.mark.parametrize("confidence", [-1, 1.01, float("nan"), float("inf"), None])
async def test_invalid_confidence(model,confidence):
    model.response.answers["trade_action"].confidence=confidence
    assert await TypeSafeService("synthetic").evaluate_decision("BTC",market(),{"sample_size":50}) is None


def test_env_guard():
    with pytest.raises(AssertionError,match="forbids .env"):
        Path(".env").read_text()

@pytest.mark.asyncio
@pytest.mark.parametrize("funding_delay,expected", [(0,"ok"),(5,"unavailable")])
async def test_ticker_source_age_not_reset(exchange, monkeypatch, funding_delay, expected):
    clock = [2_000_001_000.0]
    start = clock[0]
    monkeypatch.setattr(market_module.time,"time",lambda:clock[0])
    async def ticker(self,pair):
        return {"last":100,"timestamp":(start-119)*1000}
    async def funding(self,pair):
        clock[0] += funding_delay
        return {}
    monkeypatch.setattr(exchange,"fetch_ticker",ticker)
    monkeypatch.setattr(exchange,"fetch_funding_rate",funding)
    result = await market_module.MarketService().get_market_data("BTC")
    assert result["status"] == expected
    if expected == "ok":
        assert result["source_timestamp"] == start-119
        assert result["fetched_at"] == start
        assert result["valid_until"] == start+1
    else:
        assert result["price"] is None

@pytest.mark.asyncio
@pytest.mark.parametrize("expires", ["market","social"])
async def test_inputs_expire_during_actual_model(model,monkeypatch,expires):
    clock = [2_000_001_000.0]
    monkeypatch.setattr(model_module.time,"time",lambda:clock[0])
    quote = market()
    sample = social()
    if expires == "social":
        sample["fetched_at"] = clock[0]-599
        sample["valid_until"] = clock[0]+1
    async def get(*a,**k): return quote
    async def fetch(*a,**k): return sample
    async def slow(self,**kwargs):
        type(self).calls += 1
        clock[0] += 121 if expires == "market" else 2
        return self.response
    monkeypatch.setattr(api.market_service,"get_market_data",get)
    monkeypatch.setattr(api.twitter_service,"fetch_tweets",fetch)
    monkeypatch.setattr(api.typesafe_service,"api_key","synthetic")
    monkeypatch.setattr(model,"system_one",slow)
    result = await api.analyze_asset(api.AnalyzeRequest(symbol="BTC",sample_size=50))
    assert model.calls == 1
    assert result["decision"] is None and result["status"] == "degraded"

@pytest.mark.asyncio
@pytest.mark.parametrize("expires", ["market","social"])
async def test_endpoint_rechecks_after_model(monkeypatch,expires):
    # Independent API defense even if a model adapter returns a stale decision.
    clock = [2_000_001_000.0]
    monkeypatch.setattr(api.time,"time",lambda:clock[0])
    quote, sample = market(), social()
    if expires == "social":
        sample["fetched_at"] = clock[0]-599
        sample["valid_until"] = clock[0]+1
    async def get(*a,**k): return quote
    async def fetch(*a,**k): return sample
    async def slow(**kwargs):
        clock[0] += 121 if expires == "market" else 2
        return {"action":"BUY","confidence_pct":42}
    monkeypatch.setattr(api.market_service,"get_market_data",get)
    monkeypatch.setattr(api.twitter_service,"fetch_tweets",fetch)
    monkeypatch.setattr(api.typesafe_service,"evaluate_decision",slow)
    result = await api.analyze_asset(api.AnalyzeRequest(symbol="BTC",sample_size=50))
    assert result["decision"] is None and result["status"] == "degraded"

@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["write","fsync","replace"])
async def test_settings_failure_removes_temporary(monkeypatch,stage):
    config = api.CONFIG_PATH
    config.write_text("# original\nUNCHANGED=yes\n")
    before = config.read_bytes()
    failure = OSError(f"synthetic {stage}")
    def fail(*args,**kwargs): raise failure
    if stage == "write":
        real_fdopen = api.os.fdopen
        class FailingWriter:
            def __init__(self,fd,mode): self.stream = real_fdopen(fd,mode)
            def __enter__(self): return self
            def __exit__(self,*args): self.stream.close()
            def write(self,content):
                self.stream.write(content[:15])
                self.stream.flush()
                raise failure
        monkeypatch.setattr(api.os,"fdopen",FailingWriter)
    else:
        monkeypatch.setattr(api.os,stage,fail)
    # The original exception is retained by the writer.
    with pytest.raises(OSError) as exc:
        api.atomic_write_settings(config,"SYNTHETIC_KEY=synthetic123")
    assert exc.value is failure
    assert not list(config.parent.glob(".settings-*"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        response = await client.post("/api/v1/settings",json={"typesafe_api_key":"synthetic123","twitter_api_key":"synthetic456"})
    assert response.status_code == 500
    assert config.read_bytes() == before
    assert api.settings.TYPESAFE_API_KEY is None and api.settings.TWITTER_API_KEY is None
    assert api.typesafe_service.api_key is None and api.twitter_service.api_key is None
    assert not list(config.parent.glob(".settings-*"))

@pytest.fixture
def parsed_kraken(monkeypatch):
    # Real installed CCXT fetch_ticker + parse_ticker. Only transport reply is synthetic.
    exchange = market_module.ccxt.kraken({"enableRateLimit": False})
    instrument = {"id":"XXBTZUSD","symbol":"BTC/USD","base":"BTC","quote":"USD","spot":True,"type":"spot","precision":{"price":.01}}
    exchange.set_markets([instrument])
    clock = [2_000_001_000.0]
    delays = {"ticker":0,"funding":0}
    counts = {"ticker":0}
    monkeypatch.setattr(market_module.time,"time",lambda:clock[0])
    async def transport(*args,**kwargs):
        counts["ticker"] += 1
        clock[0] += delays["ticker"]
        return {"result":{"XXBTZUSD":{"a":["101","1","1"],"b":["99","1","1"],"c":["100","1"],"v":["10","20"],"p":["100","100"],"t":[1,2],"l":["90","90"],"h":["110","110"],"o":"100"}}}
    monkeypatch.setattr(exchange,"publicGetTicker",transport)
    # Closed candles/futures are irrelevant to parser compatibility and remain synthetic.
    async def candles(*args,**kwargs): return await Exchange().fetch_ohlcv()
    monkeypatch.setattr(exchange,"fetch_ohlcv",candles)
    class Futures(Exchange):
        async def fetch_funding_rate(self,*args):
            clock[0] += delays["funding"]
            return {}
    monkeypatch.setattr(market_module.ccxt,"kraken",lambda *args:exchange)
    monkeypatch.setattr(market_module.ccxt,"krakenfutures",Futures)
    return exchange,clock,delays,counts

@pytest.mark.asyncio
async def test_real_kraken_parser_without_source_timestamp(parsed_kraken):
    _,clock,delays,_ = parsed_kraken
    start = clock[0]
    delays["ticker"] = 3
    result = await market_module.MarketService().get_market_data("BTC")
    assert result["status"] == "ok" and result["price"] == 100
    assert result["source_timestamp"] is None and result["freshness_basis"] == "request_start"
    assert result["request_started_at"] == start and result["fetched_at"] == start+3
    assert result["valid_until"] == start+120

@pytest.mark.asyncio
@pytest.mark.parametrize("ticker_delay,funding_delay", [(121,0),(100,21)])
async def test_request_start_deadline_includes_all_fetches(parsed_kraken,ticker_delay,funding_delay):
    _,_,delays,_ = parsed_kraken
    delays.update(ticker=ticker_delay,funding=funding_delay)
    result = await market_module.MarketService().get_market_data("BTC")
    assert result["status"] == "unavailable" and result["price"] is None

@pytest.mark.asyncio
async def test_no_timestamp_cache_keeps_deadline_and_expires(parsed_kraken):
    _,clock,delays,counts = parsed_kraken
    delays["ticker"] = 100
    service = market_module.MarketService()
    result = await service.get_market_data("BTC")
    deadline = result["valid_until"]
    clock[0] += 10
    cached = await service.get_market_data("BTC")
    assert cached["valid_until"] == deadline and counts["ticker"] == 1
    clock[0] += 11
    delays["ticker"] = 121
    expired = await service.get_market_data("BTC")
    assert expired["status"] == "unavailable" and counts["ticker"] == 2
    assert result["valid_until"] == deadline

@pytest.mark.asyncio
async def test_no_timestamp_expires_during_model(parsed_kraken,model,monkeypatch):
    _,clock,_,_ = parsed_kraken
    quote = await market_module.MarketService().get_market_data("BTC")
    async def slow(self,**kwargs):
        clock[0] += 121
        return self.response
    monkeypatch.setattr(model,"system_one",slow)
    result = await TypeSafeService("synthetic").evaluate_decision("BTC",quote,{"sample_size":50})
    assert result is None

@pytest.mark.asyncio
@pytest.mark.parametrize("timestamp", [float("nan"),float("inf"),2_000_001_001_000])
async def test_present_bad_source_timestamp_not_treated_as_absent(exchange,monkeypatch,timestamp):
    monkeypatch.setattr(market_module.time,"time",lambda:2_000_001_000.0)
    async def ticker(self,*args): return {"last":100,"timestamp":timestamp}
    monkeypatch.setattr(exchange,"fetch_ticker",ticker)
    result = await market_module.MarketService().get_market_data("BTC")
    assert result["status"] == "unavailable"
