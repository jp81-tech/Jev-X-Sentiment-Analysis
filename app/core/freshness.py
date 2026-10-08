"""Validity is an absolute deadline, never reset by later processing."""
import math
import time


def within_validity(data, now=None):
    now = time.time() if now is None else now
    start, end = data.get("fetched_at"), data.get("valid_until")
    return (all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                for v in (start, end)) and start <= now < end)


def market_is_fresh(data, now=None):
    now = time.time() if now is None else now
    source_at, start = data.get("source_timestamp"), data.get("request_started_at")
    if (not within_validity(data, now) or not isinstance(start, (int, float))
            or isinstance(start, bool) or not math.isfinite(start)
            or not start <= data["fetched_at"] <= now or not 0 <= now - start < 120):
        return False
    if source_at is None:
        return data.get("freshness_basis") == "request_start" and data["valid_until"] <= start + 120
    return (data.get("freshness_basis") == "source_timestamp"
            and isinstance(source_at, (int, float)) and not isinstance(source_at, bool)
            and math.isfinite(source_at) and 0 <= now - source_at < 120
            and data["valid_until"] <= min(start, source_at) + 120)


def social_is_fresh(data, now=None):
    now = time.time() if now is None else now
    age = data.get("publication_max_age_seconds")
    tweets = data.get("tweets", [])
    return (within_validity(data, now) and isinstance(age, (int, float))
            and not isinstance(age, bool) and math.isfinite(age) and age > 0
            and bool(tweets) and all(isinstance(t.get("timestamp_epoch"), (int, float))
                and not isinstance(t["timestamp_epoch"], bool) and math.isfinite(t["timestamp_epoch"])
                and 0 <= now - t["timestamp_epoch"] < age for t in tweets))
