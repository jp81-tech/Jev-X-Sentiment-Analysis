import pytest
import httpx
from test_pipeline import app, api, market_module, TypeSafeService, market, social, model, exchange


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("percentage", float("nan")), ("percentage", float("inf")),
    ("percentage", -100.01), ("percentage", True),
    ("high", float("inf")), ("low", 0),
    ("quoteVolume", -1), ("baseVolume", float("nan")),
])
async def test_bad_spot_metadata_blocks_asgi_model(exchange, model, monkeypatch, field, value):
    original = exchange.fetch_ticker
    async def ticker(self, *args):
        return dict(await original(self, *args), **{field: value})
    async def fetch(*args, **kwargs): return social()
    monkeypatch.setattr(exchange, "fetch_ticker", ticker)
    monkeypatch.setattr(api.twitter_service, "fetch_tweets", fetch)
    monkeypatch.setattr(api.typesafe_service, "api_key", "synthetic")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v1/analyze", json={"symbol": "BTC", "sample_size": 50})
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "degraded"
    assert result["market"]["status"] == "unavailable"
    assert result["decision"] is None and model.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("change_24h_pct", float("nan")), ("change_24h_pct", -101),
    ("volume_24h_usd", float("inf")), ("funding_rate_pct", float("nan")),
    ("open_interest_usd", -1), ("rsi_14", 101),
])
async def test_model_rejects_bad_market_metadata(model, field, value):
    quote = dict(market(), **{field: value})
    assert await TypeSafeService("synthetic").evaluate_decision("BTC", quote, {"sample_size": 50}) is None
    assert model.calls == 0


@pytest.mark.asyncio
async def test_absent_spot_metadata_stays_unknown(exchange, model, monkeypatch):
    quote = await market_module.MarketService().get_market_data("BTC")
    assert quote["status"] == "ok"
    for key in ("change_24h_pct", "high_24h", "low_24h", "volume_24h_usd"):
        assert quote[key] is None
    assert quote["momentum_bucket"] == "unknown"
    observed = {}
    original = model.system_one
    async def capture(self, **kwargs):
        observed.update(kwargs["state"]["market"])
        return await original(self, **kwargs)
    monkeypatch.setattr(model, "system_one", capture)
    assert await TypeSafeService("synthetic").evaluate_decision("BTC", quote, {"sample_size": 50}) is not None
    assert observed["change_24h_pct"] is None and observed["volume_24h_usd"] is None
    assert observed["momentum_bucket"] == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"fundingRate": float("nan")},
    {"fundingRate": 1e308},
    {"fundingRate": .001, "info": {"openInterest": float("inf")}},
    {"fundingRate": .001, "info": {"openInterest": -1}},
])
async def test_invalid_optional_futures_data_cleared(exchange, monkeypatch, payload):
    async def funding(*args): return payload
    monkeypatch.setattr(exchange, "fetch_funding_rate", funding)
    quote = await market_module.MarketService().get_market_data("BTC")
    assert quote["status"] == "ok"
    assert quote["funding_rate_pct"] is None and quote["open_interest_usd"] is None
    assert quote["has_perpetuals"] is False


@pytest.mark.asyncio
async def test_zero_metadata_is_preserved(exchange, monkeypatch):
    original = exchange.fetch_ticker
    async def ticker(self, *args):
        return dict(await original(self, *args), percentage=0, quoteVolume=0)
    monkeypatch.setattr(exchange, "fetch_ticker", ticker)
    quote = await market_module.MarketService().get_market_data("BTC")
    assert quote["status"] == "ok"
    assert quote["change_24h_pct"] == quote["volume_24h_usd"] == 0
    assert quote["momentum_bucket"] == "neutral"


@pytest.mark.asyncio
async def test_invalid_cached_metadata_refetched(exchange):
    cached = dict(market(), change_24h_pct=float("nan"))
    await market_module.market_cache.set("market_BTC", cached, ttl=60)
    quote = await market_module.MarketService().get_market_data("BTC")
    assert quote is not cached and quote["status"] == "ok"
    assert quote["change_24h_pct"] is None


@pytest.mark.asyncio
async def test_inverted_high_low_rejected(exchange, monkeypatch):
    original = exchange.fetch_ticker
    async def ticker(self, *args):
        return dict(await original(self, *args), high=1, low=2)
    monkeypatch.setattr(exchange, "fetch_ticker", ticker)
    quote = await market_module.MarketService().get_market_data("BTC")
    assert quote["status"] == "unavailable"
