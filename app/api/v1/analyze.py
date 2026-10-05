from fastapi import APIRouter, HTTPException, Request, Header
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any
import logging
import asyncio
import re
import os
import time
import tempfile
from pathlib import Path

from app.core.config import settings, CONFIG_PATH
from app.core.freshness import market_is_fresh, within_validity
from app.core.market_validation import market_numbers_valid
from app.services.market_service import market_service
from app.services.twitter_service import twitter_service
from app.services.stats_service import stats_service
from app.services.typesafe_service import typesafe_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["Analysis"])

KEY_REGEX = re.compile(r"^[A-Za-z0-9_\-\.]{8,128}$")
settings_lock = asyncio.Lock()

SYMBOL_REGEX = re.compile(r"^[A-Z0-9]{1,15}$")


class AnalyzeRequest(BaseModel):
    symbol: str = Field(..., json_schema_extra={"example": "BTC"}, description="Crypto symbol (e.g. BTC, SOL, ETH)")
    sample_size: int = Field(100, ge=50, le=1000, description="Tweet sample count: 50, 100, 250, 500, or 1000")


@router.post("/analyze")
async def analyze_asset(req: AnalyzeRequest) -> Dict[str, Any]:
    """
    On-demand market intelligence & decision generation.
    1. Fetches live market data (CCXT Kraken / Kraken Futures).
    2. Ingests N tweets (TwitterAPI.io with pagination).
    3. Runs Tier 1 statistical aggregation & stratified sampling.
    4. Evaluates state with TypeSafe Jev System One.
    """
    sym = req.symbol.strip().upper().replace("$", "")
    if not SYMBOL_REGEX.match(sym):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid crypto symbol '{req.symbol}'. Symbol must be 1-15 alphanumeric characters."
        )

    sample_size = req.sample_size

    try:
        # Run market data & tweet ingestion concurrently
        market_task = market_service.get_market_data(sym)
        tweets_task = twitter_service.fetch_tweets(sym, target_count=sample_size)

        market_data, twitter_res = await asyncio.gather(market_task, tweets_task)

        tweets = twitter_res.get("tweets", [])

        # Tier 1: Statistical processing
        social_stats = stats_service.process_tweets(tweets)

        decision = None
        fresh_market = (market_data.get("status") == "ok" and market_data.get("source") == "kraken"
                        and not market_data.get("is_fallback")
                        and market_is_fresh(market_data) and market_numbers_valid(market_data))
        fresh_social = (twitter_res.get("status") == "ok" and twitter_res.get("source") == "twitterapi.io"
                        and not twitter_res.get("is_mock")
                        and len(tweets) == sample_size
                        and len({t.get("id") for t in tweets}) == sample_size
                        and all(t.get("id") and t.get("source") == "twitterapi.io" for t in tweets)
                        and within_validity(twitter_res))
        social_stats.update({"fetched_at": twitter_res.get("fetched_at"), "valid_until": twitter_res.get("valid_until")})
        if fresh_market and fresh_social:
            decision = await typesafe_service.evaluate_decision(symbol=sym, market_data=market_data, social_stats=social_stats)
        # Check again after the awaited model; both inputs may have expired.
        if not market_is_fresh(market_data) or not within_validity(twitter_res):
            decision = None
        status = "success" if decision else ("degraded" if tweets else "unavailable")

        return {
            "symbol": sym,
            "status": status,
            "social": {k: v for k, v in twitter_res.items() if k != "tweets"},
            "model_status": "ok" if decision else "unavailable",
            "market": market_data,
            "social_stats": social_stats,
            "decision": decision,
            "tweets_sample": tweets[:100],  # Return up to 100 representative tweets for UI explorer
            "is_twitter_mock": twitter_res.get("is_mock", False),
            "is_typesafe_mock": bool(decision and decision.get("is_mock"))
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error analyzing {sym}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to analyze {sym}: {str(e)}")


@router.get("/market/{symbol}")
async def get_market_summary(symbol: str) -> Dict[str, Any]:
    """Quick market data lookup for an asset."""
    sym = symbol.strip().upper().replace("$", "")
    if not SYMBOL_REGEX.match(sym):
        raise HTTPException(status_code=400, detail=f"Invalid symbol '{symbol}'. Must be alphanumeric.")
    try:
        return await market_service.get_market_data(sym)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class SettingsUpdate(BaseModel):
    typesafe_api_key: Optional[str] = None
    twitter_api_key: Optional[str] = None


@router.get("/settings")
async def get_settings_status() -> Dict[str, Any]:
    return {
        "has_typesafe_key": bool(settings.TYPESAFE_API_KEY),
        "has_twitter_key": bool(settings.TWITTER_API_KEY)
    }


@router.post("/settings")
async def update_settings(
    payload: SettingsUpdate,
    request: Request,
    x_admin_token: Optional[str] = Header(None, alias="X-Admin-Token")
) -> Dict[str, Any]:
    """
    Update API keys with authentication and input sanitization.
    Permits access from localhost or with a valid X-Admin-Token header.
    """
    client_host = request.client.host if request.client else "unknown"
    is_local = client_host in ("127.0.0.1", "::1", "localhost", "testclient")
    has_valid_admin_token = bool(settings.ADMIN_TOKEN and x_admin_token == settings.ADMIN_TOKEN)

    if not (is_local or has_valid_admin_token):
        raise HTTPException(
            status_code=403,
            detail="Forbidden: Settings modification is restricted to localhost or authenticated requests."
        )

    updates = {}
    for field, key in (("typesafe_api_key", "TYPESAFE_API_KEY"), ("twitter_api_key", "TWITTER_API_KEY")):
        value = getattr(payload, field)
        if value is not None:
            value = value.strip()
            if not KEY_REGEX.fullmatch(value):
                raise HTTPException(status_code=400, detail="Invalid API key format.")
            updates[key] = value
    async with settings_lock:
        env_path = CONFIG_PATH
        try:
            # Preserve unrelated configuration verbatim. No runtime mutation before replace.
            lines = env_path.read_text().splitlines() if env_path.exists() else []
            lines = [line for line in lines if line.split("=", 1)[0].strip() not in updates]
            content = "\n".join(lines + [f"{key}={value}" for key, value in updates.items()]) + "\n"
            atomic_write_settings(env_path, content)
        except OSError:
            raise HTTPException(status_code=500, detail="Could not persist settings; runtime unchanged.")
        for key, value in updates.items():
            setattr(settings, key, value)
        typesafe_service.api_key = settings.TYPESAFE_API_KEY
        twitter_service.api_key = settings.TWITTER_API_KEY
    return {"status": "success", "has_typesafe_key": bool(settings.TYPESAFE_API_KEY), "has_twitter_key": bool(settings.TWITTER_API_KEY)}


def atomic_write_settings(path: Path, content: str):
    # mkstemp uses mode 0600. Same directory guarantees atomic replacement.
    fd, temporary = tempfile.mkstemp(prefix=".settings-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        # Do not retain credentials on handled errors or mask the original failure.
        try:
            os.unlink(temporary)
        except OSError:
            logger.warning("Could not remove failed settings temporary file")
        raise
