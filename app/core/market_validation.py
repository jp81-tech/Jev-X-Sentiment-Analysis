"""Numeric contract shared by market ingestion and the model boundary."""
import math


def finite_number(value, *, minimum=None, maximum=None, positive=False, optional=False):
    if value is None and optional:
        return None
    if isinstance(value, bool):
        raise ValueError("Boolean is not a market number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Invalid market number") from exc
    if (not math.isfinite(number) or (positive and number <= 0)
            or (minimum is not None and number < minimum)
            or (maximum is not None and number > maximum)):
        raise ValueError("Market number outside its valid range")
    return number


def market_numbers_valid(data):
    rules = {
        "price": {"positive": True},
        "rsi_14": {"minimum": 0, "maximum": 100},
        "tick_size": {"positive": True, "optional": True},
        "change_24h_pct": {"minimum": -100, "optional": True},
        "high_24h": {"positive": True, "optional": True},
        "low_24h": {"positive": True, "optional": True},
        "volume_24h_usd": {"minimum": 0, "optional": True},
        "funding_rate_pct": {"optional": True},
        "open_interest_usd": {"minimum": 0, "optional": True},
    }
    try:
        for key, limits in rules.items():
            value = data.get(key)
            # Ingestion converts provider strings; internal payloads must be numeric.
            if value is not None and not isinstance(value, (int, float)):
                return False
            finite_number(value, **limits)
        high, low = data.get("high_24h"), data.get("low_24h")
        return high is None or low is None or high >= low
    except ValueError:
        return False


def momentum_bucket(change):
    if change is None:
        return "unknown"
    return "bullish" if change > 3 else ("bearish" if change < -3 else "neutral")
