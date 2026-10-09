import asyncio
import math
import hashlib
import httpx
import logging
from typing import List, Dict, Any, Optional
import time
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from app.core.config import settings
from app.core.cache import social_cache
from app.core.freshness import within_validity, social_is_fresh
from app.core.database import db

logger = logging.getLogger(__name__)

TWITTER_API_URL = "https://api.twitterapi.io/twitter/tweet/advanced_search"
SYMBOL_REGEX = re.compile(r"^[A-Z0-9]{1,15}$")


FETCH_BUDGET_SECONDS = 75
PAGE_ATTEMPTS = 4
_monotonic = time.monotonic
_sleep = asyncio.sleep


def _provider_count(data, primary, alternate):
    value = data.get(primary)
    if value is None:
        value = data.get(alternate)
    if value is None:
        return 0
    if type(value) not in (int, float, str) or isinstance(value, str) and not value.isdigit():
        raise ValueError("Invalid count")
    result = int(value)
    if result < 0 or isinstance(value, float) and value != result:
        raise ValueError("Invalid count")
    return result


class _MalformedResponse(Exception):
    pass


class _RateLimited(Exception):
    pass


class _RateLimitTimeout(Exception):
    pass


class _RequestGate:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.not_before = 0.0
        self.users = 0


# Share cooldowns across sequential and overlapping fetches by credential hash.
# Expired unused entries are removed; saturation refuses new keys, never evicts a cooldown.
_request_gates = {}
MAX_GATES = 256


def request_gate(key):
    now = _monotonic()
    for old, gate in list(_request_gates.items()):
        if gate.users == 0 and gate.not_before <= now:
            del _request_gates[old]
    if key not in _request_gates:
        if len(_request_gates) >= MAX_GATES:
            return None  # Never discard another key's active cooldown.
        _request_gates[key] = _RequestGate()
    return _request_gates[key]


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


def parse_twitter_timestamp(ts_str: Optional[str]) -> Optional[int]:
    """Missing, naive, malformed and out-of-range dates remain unknown."""
    if not isinstance(ts_str, str) or not ts_str:
        return None
    try:
        try:
            dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except ValueError:
            dt = parsedate_to_datetime(ts_str)
        if dt.tzinfo is None:
            return None
        return int(dt.astimezone(timezone.utc).timestamp())
    except (ValueError, TypeError, OverflowError, OSError):
        return None


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
        window_seconds = max(1, settings.SOCIAL_WINDOW_HOURS) * 3600
        cache_key = f"tweets_v3_{sym}_{target_count}_{key}_{window_seconds}"
        cached = await social_cache.get(cache_key)
        if cached and social_is_fresh(cached):
            return cached
        fetched_at = time.time()
        deadline = _monotonic() + FETCH_BUDGET_SECONDS
        has_key = bool(self.api_key and self.api_key.strip())
        gate = request_gate(key) if has_key else None
        rejected = {"unknown_date": 0, "outside_window": 0, "future_date": 0}
        window_start = fetched_at - window_seconds
        query += f" since_time:{int(window_start)} until_time:{int(fetched_at)}"
        tweets, seen, cursors = [], set(), set()
        cursor, reason, calls = None, "missing_key", 0
        if gate is None and has_key:
            reason = "rate_limit_capacity"
        if gate is not None and self.api_key and self.api_key.strip():
            gate.users += 1
            try:
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
                            try:
                                data = res.json()
                                if not isinstance(data, dict):
                                    raise ValueError("Invalid response schema")
                                raw = data.get("tweets") if "tweets" in data else data.get("data")
                                if not isinstance(raw, list):
                                    raise ValueError("Invalid tweet collection")
                                if "has_next_page" in data and type(data["has_next_page"]) is not bool:
                                    raise ValueError("Invalid pagination flag")
                                before = len(seen)
                                for t in raw:
                                    if not isinstance(t, dict):
                                        raise ValueError("Invalid tweet schema")
                                    tid = t.get("id")
                                    if not isinstance(tid, str) or not tid.strip() or tid in seen:
                                        continue
                                    seen.add(tid)
                                    author = t.get("author")
                                    if author is None:
                                        author = {}
                                    if not isinstance(author, dict):
                                        raise ValueError("Invalid author schema")
                                    text = t.get("text", "")
                                    if not isinstance(text, str):
                                        raise ValueError("Invalid text schema")
                                    for name in ("userName", "username"):
                                        if author.get(name) is not None and not isinstance(author[name], str):
                                            raise ValueError("Invalid username schema")
                                    created = t.get("createdAt") or t.get("created_at") or ""
                                    published = parse_twitter_timestamp(created)
                                    if published is None:
                                        rejected["unknown_date"] += 1
                                        continue
                                    if published > fetched_at:
                                        rejected["future_date"] += 1
                                        continue
                                    if published <= window_start:
                                        rejected["outside_window"] += 1
                                        continue
                                    tweets.append({
                                        "id": tid, "text": text, "source": "twitterapi.io",
                                        "created_at": created, "timestamp_epoch": published,
                                        "likes": _provider_count(t, "likeCount", "likes"),
                                        "retweets": _provider_count(t, "retweetCount", "retweets"),
                                        "replies": _provider_count(t, "replyCount", "replies"),
                                        "author_username": author.get("userName") or author.get("username") or "anonymous",
                                        "author_followers": _provider_count(author, "followers", "followersCount"),
                                        "author_verified": bool(author.get("isBlueVerified") or author.get("verified"))
                                    })
                                    if len(tweets) == target_count:
                                        break
                            except (ValueError, TypeError, OverflowError):
                                raise _MalformedResponse() from None
                            if len(tweets) == target_count:
                                reason = "target_reached"
                                break
                            if not raw or data.get("has_next_page") is False:
                                reason = "end_of_results"
                                break
                            next_cursor = data.get("next_cursor") or data.get("cursor")
                            if not isinstance(next_cursor, str) or not next_cursor.strip() or next_cursor in cursors or len(seen) == before:
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
                        except httpx.HTTPStatusError as error:
                            code = error.response.status_code
                            reason = "payment_required" if code == 402 else "http_error" if 400 <= code <= 599 else "provider_error"
                            logger.warning("Twitter category=%s http_code=%s", reason, code)
                            break
                        except httpx.TimeoutException:
                            reason = "fetch_timeout"
                            logger.warning("Twitter category=fetch_timeout")
                            break
                        except httpx.DecodingError:
                            reason = "malformed_response"
                            logger.warning("Twitter category=malformed_response")
                            break
                        except httpx.RequestError:
                            reason = "transport_error"
                            logger.warning("Twitter category=transport_error")
                            break
                        except _MalformedResponse:
                            reason = "malformed_response"
                            logger.warning("Twitter category=malformed_response")
                            break
                        except Exception:
                            logger.warning("Twitter category=provider_error")
                            reason = "provider_error"
                            break
            finally:
                gate.users -= 1
        if tweets:
            await db.save_tweets(sym, tweets, source="twitterapi.io")
        # Never silently top up a fresh search from historical/unknown data.
        status = "ok" if len(tweets) == target_count else ("partial" if tweets else "unavailable")
        result = {"symbol": sym, "status": status, "reason": reason, "source": "twitterapi.io",
                  "fetched_at": fetched_at, "valid_until": min([fetched_at + settings.CACHE_TTL_SECONDS] + [t["timestamp_epoch"] + window_seconds for t in tweets]), "count": len(tweets), "target_count": target_count,
                  "newly_fetched_count": len(tweets), "api_pages_called": calls, "early_stopped": False,
                  "publication_window_start": window_start, "publication_window_end": fetched_at,
                  "publication_max_age_seconds": window_seconds, "rejected_dates": rejected,
                  "oldest_publication_at": min((t["timestamp_epoch"] for t in tweets), default=None),
                  "newest_publication_at": max((t["timestamp_epoch"] for t in tweets), default=None),
                  "yield_deficit": len(tweets) < target_count, "is_mock": False, "tweets": tweets}
        if status == "ok":
            await social_cache.set(cache_key, result, ttl=settings.CACHE_TTL_SECONDS)
        return result


twitter_service = TwitterService()
