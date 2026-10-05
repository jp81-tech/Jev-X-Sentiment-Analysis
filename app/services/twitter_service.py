import asyncio
import math
import weakref
import httpx
import logging
from typing import List, Dict, Any, Optional
import time
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from app.core.config import settings
from app.core.cache import social_cache
from app.core.freshness import within_validity
from app.core.database import db

logger = logging.getLogger(__name__)

TWITTER_API_URL = "https://api.twitterapi.io/twitter/tweet/advanced_search"
SYMBOL_REGEX = re.compile(r"^[A-Z0-9]{1,15}$")


FETCH_BUDGET_SECONDS = 75
PAGE_ATTEMPTS = 4
_monotonic = time.monotonic
_sleep = asyncio.sleep


class _RateLimited(Exception):
    pass


class _RateLimitTimeout(Exception):
    pass


class _RequestGate:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.not_before = 0.0


# Share cooldowns among overlapping fetches using the same credential, without
# retaining credentials or unbounded inactive per-key state.
_request_gates = weakref.WeakValueDictionary()


def retry_delay(value, attempt):
    fallback = 5.0 * (2 ** attempt)
    if isinstance(value, str):
        try:
            seconds = float(value) if value.strip().isdigit() else parsedate_to_datetime(value).timestamp() - time.time()
            if math.isfinite(seconds) and seconds >= 0:
                return max(5.0, seconds)
        except (ValueError, TypeError, OverflowError):
            pass
    return fallback


def parse_twitter_timestamp(ts_str: Optional[str]) -> int:
    """Parse various Twitter date formats into unix integer seconds."""
    if not ts_str:
        return int(time.time())
    try:
        # ISO 8601 (e.g. 2026-09-22T04:00:00.000Z)
        if ts_str.endswith("Z"):
            return int(datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp())
        elif "T" in ts_str:
            return int(datetime.fromisoformat(ts_str).timestamp())
        # RFC 2822 (e.g. Tue Sep 22 04:00:00 +0000 2026)
        dt = parsedate_to_datetime(ts_str)
        return int(dt.timestamp())
    except Exception:
        return int(time.time())


class TwitterService:
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or settings.TWITTER_API_KEY

    def _build_query(self, symbol: str) -> str:
        sym = symbol.upper().replace("$", "").strip()
        if not SYMBOL_REGEX.match(sym):
            raise ValueError(f"Invalid symbol '{symbol}' for query builder. Must be 1-15 alphanumeric characters.")

        names = {
            "BTC": "Bitcoin",
            "SOL": "Solana",
            "ETH": "Ethereum",
            "DOGE": "Dogecoin",
            "XRP": "Ripple",
            "ADA": "Cardano",
            "AVAX": "Avalanche",
            "NEAR": "Near",
            "SUI": "Sui"
        }
        full_name = names.get(sym, sym)
        if sym == full_name:
            return f"${sym} lang:en -is:retweet min_faves:2"
        return f"(${sym} OR {full_name}) lang:en -is:retweet min_faves:2"

    async def fetch_tweets(self, symbol: str, target_count: int = 100) -> Dict[str, Any]:
        sym = symbol.upper().replace("$", "").strip()
        query = self._build_query(sym)
        if not 1 <= target_count <= 1000:
            raise ValueError("target_count must be between 1 and 1000")
        # Cache includes credential generation without exposing the credential.
        import hashlib
        key = hashlib.sha256((self.api_key or "").encode()).hexdigest()
        cache_key = f"tweets_v2_{sym}_{target_count}_{key}"
        cached = await social_cache.get(cache_key)
        if cached and within_validity(cached):
            return cached
        fetched_at = time.time()
        deadline = _monotonic() + FETCH_BUDGET_SECONDS
        gate = _request_gates.setdefault(key, _RequestGate())
        tweets, seen, cursors = [], set(), set()
        cursor, reason, calls = None, "missing_key", 0
        if self.api_key and self.api_key.strip():
            reason = "page_limit"
            async with httpx.AsyncClient(timeout=settings.REQUEST_TIMEOUT) as client:
                for _ in range(100):
                    params = {"query": query, "queryType": "Latest"}
                    if cursor:
                        params["cursor"] = cursor
                    try:
                        for attempt in range(PAGE_ATTEMPTS):
                            remaining = deadline - _monotonic()
                            if remaining <= 0:
                                raise _RateLimitTimeout()
                            # Includes waiting for another fetch's in-flight request.
                            async with asyncio.timeout(remaining):
                                async with gate.lock:
                                    delay = max(0.0, gate.not_before - _monotonic())
                                    if delay >= deadline - _monotonic():
                                        raise _RateLimitTimeout()
                                    if delay:
                                        await _sleep(delay)
                                    if _monotonic() >= deadline:
                                        raise _RateLimitTimeout()
                                    calls += 1
                                    res = await client.get(TWITTER_API_URL, headers={"X-API-Key": self.api_key}, params=params)
                                    if getattr(res, "status_code", None) == 429:
                                        gate.not_before = _monotonic() + retry_delay(res.headers.get("Retry-After"), attempt)
                                    else:
                                        break
                            if attempt == PAGE_ATTEMPTS - 1:
                                raise _RateLimited()
                        res.raise_for_status()
                        data = res.json()
                        raw = data.get("tweets") or data.get("data") or []
                        if not isinstance(raw, list):
                            raise ValueError("Invalid tweet collection")
                        before = len(tweets)
                        for t in raw:
                            tid = t.get("id")
                            if not isinstance(tid, str) or not tid.strip() or tid in seen:
                                continue
                            seen.add(tid)
                            author = t.get("author") or {}
                            created = t.get("createdAt") or t.get("created_at") or ""
                            tweets.append({
                                "id": tid, "text": t.get("text", ""), "source": "twitterapi.io",
                                "created_at": created, "timestamp_epoch": parse_twitter_timestamp(created),
                                "likes": int(t.get("likeCount") or t.get("likes") or 0),
                                "retweets": int(t.get("retweetCount") or t.get("retweets") or 0),
                                "replies": int(t.get("replyCount") or t.get("replies") or 0),
                                "author_username": author.get("userName") or author.get("username") or "anonymous",
                                "author_followers": int(author.get("followers") or author.get("followersCount") or 0),
                                "author_verified": bool(author.get("isBlueVerified") or author.get("verified"))
                            })
                            if len(tweets) == target_count:
                                break
                        if len(tweets) == target_count:
                            reason = "target_reached"
                            break
                        if not raw or data.get("has_next_page") is False:
                            reason = "end_of_results"
                            break
                        next_cursor = data.get("next_cursor") or data.get("cursor")
                        if not isinstance(next_cursor, str) or not next_cursor.strip() or next_cursor in cursors or len(tweets) == before:
                            reason = "pagination_no_progress"
                            break
                        cursors.add(next_cursor)
                        cursor = next_cursor
                    except _RateLimited:
                        reason = "rate_limited"
                        break
                    except (_RateLimitTimeout, TimeoutError):
                        reason = "rate_limit_timeout" if gate.not_before > _monotonic() else "fetch_timeout"
                        break
                    except Exception:
                        logger.warning("Twitter provider unavailable or returned malformed data")
                        reason = "provider_error"
                        break
        if tweets:
            await db.save_tweets(sym, tweets, source="twitterapi.io")
        # Never silently top up a fresh search from historical/unknown data.
        status = "ok" if len(tweets) == target_count else ("partial" if tweets else "unavailable")
        result = {"symbol": sym, "status": status, "reason": reason, "source": "twitterapi.io",
                  "fetched_at": fetched_at, "valid_until": fetched_at + settings.CACHE_TTL_SECONDS, "count": len(tweets), "target_count": target_count,
                  "newly_fetched_count": len(tweets), "api_pages_called": calls, "early_stopped": False,
                  "yield_deficit": len(tweets) < target_count, "is_mock": False, "tweets": tweets}
        if status == "ok":
            await social_cache.set(cache_key, result, ttl=settings.CACHE_TTL_SECONDS)
        return result


twitter_service = TwitterService()
