import ccxt.async_support as ccxt
import logging
import math
from typing import Dict, Any, List, Optional
import time
from app.core.cache import market_cache
from app.core.freshness import market_is_fresh
from app.core.market_validation import finite_number, market_numbers_valid, momentum_bucket

logger = logging.getLogger(__name__)


def calculate_rsi(prices: List[float], period: int = 14) -> Optional[float]:
    """Calculate Relative Strength Index (RSI) across price series."""
    if period < 1 or len(prices) < period + 1 or any(not math.isfinite(p) or p <= 0 for p in prices):
        return None
    gains = []
    losses = []
    for i in range(1, len(prices)):
        change = prices[i] - prices[i - 1]
        if change > 0:
            gains.append(change)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(abs(change))

    if len(gains) < period:
        return None

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_gain == 0 and avg_loss == 0:
        return 50.0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 2)


class MarketService:
    async def get_market_data(self, symbol: str) -> Dict[str, Any]:
        sym = symbol.upper().replace("$", "")
        pair = f"{sym}/USD"
        cache_key = f"market_{sym}"

        cached = await market_cache.get(cache_key)
        if cached and market_is_fresh(cached) and market_numbers_valid(cached):
            return cached

        # Use Kraken for spot ticker & OHLCV, Kraken Futures for real perpetual funding rate & open interest
        exchange = ccxt.kraken({"enableRateLimit": True, "timeout": 8000})
        futures_exchange = ccxt.krakenfutures({"enableRateLimit": True, "timeout": 8000})

        try:
            # 1. Fetch Spot Ticker
            request_started_at = time.time()
            ticker = await exchange.fetch_ticker(pair)
            raw_price = ticker.get("last")
            price = finite_number(ticker.get("close") if raw_price is None else raw_price, positive=True)
            now = time.time()
            ticker_ts = ticker.get("timestamp")
            source_timestamp = float(ticker_ts) / 1000 if ticker_ts is not None else None
            if source_timestamp is not None and (not math.isfinite(source_timestamp) or not 0 <= now - source_timestamp < 120):
                raise ValueError("Invalid or stale ticker timestamp")
            if not 0 <= now - request_started_at < 120:
                raise ValueError("Ticker request exceeded validity window")
            ticker_fetched_at = now
            freshness_basis = "request_start" if source_timestamp is None else "source_timestamp"
            valid_until = (request_started_at if source_timestamp is None else min(request_started_at, source_timestamp)) + 120
            instrument = exchange.market(pair)
            precision = instrument.get("precision", {}).get("price")
            if precision is None:
                raise ValueError("Missing tick size")
            if exchange.precisionMode == ccxt.TICK_SIZE:
                tick_size = float(precision)
            elif exchange.precisionMode == ccxt.DECIMAL_PLACES:
                tick_size = 10.0 ** -int(precision)
            else:
                raise ValueError("Unsupported price precision")
            if not math.isfinite(tick_size) or tick_size <= 0:
                raise ValueError("Invalid tick size")
            change_24h = finite_number(ticker.get("percentage"), minimum=-100, optional=True)
            high_24h = finite_number(ticker.get("high"), positive=True, optional=True)
            low_24h = finite_number(ticker.get("low"), positive=True, optional=True)
            if high_24h is not None and low_24h is not None and high_24h < low_24h:
                raise ValueError("Invalid high/low range")
            volume_24h = finite_number(ticker.get("quoteVolume"), minimum=0, optional=True)
            base_volume = finite_number(ticker.get("baseVolume"), minimum=0, optional=True)
            if volume_24h is None and base_volume is not None:
                volume_24h = finite_number(base_volume * price, minimum=0)

            # 2. Fetch OHLCV candles (last 48 1h candles)
            candles_raw = await exchange.fetch_ohlcv(pair, timeframe="1h", limit=48)
            candles = []
            close_prices = []
            for c in sorted(candles_raw, key=lambda c: c[0]):
                if c[0] / 1000 + 3600 > now:
                    continue  # Exclude the still-open candle.
                if any(not math.isfinite(float(v)) or float(v) <= 0 for v in c[1:5]):
                    raise ValueError("Invalid candle")
                candles.append({
                    "time": int(c[0] // 1000),  # TradingView expects unix seconds
                    "open": float(c[1]),
                    "high": float(c[2]),
                    "low": float(c[3]),
                    "close": float(c[4]),
                    "volume": finite_number(c[5], minimum=0, optional=True)
                })
                close_prices.append(float(c[4]))

            rsi = calculate_rsi(close_prices, 14)
            if rsi is None or not candles or not 0 <= now - (candles[-1]["time"] + 3600) <= 3600:
                raise ValueError("Insufficient or stale closed candles")
            if any(b["time"] - a["time"] != 3600 for a, b in zip(candles, candles[1:])):
                raise ValueError("Missing or duplicate candles")

            # 3. Fetch Real Perpetuals Funding Rate & Open Interest
            funding_rate_pct = None
            open_interest_usd = None
            has_perpetuals = False

            try:
                futures_pair = f"{sym}/USD:USD"
                fr_info = await futures_exchange.fetch_funding_rate(futures_pair)
                rate = finite_number(fr_info.get("fundingRate"), optional=True)
                info_block = fr_info.get("info") or {}
                oi = finite_number(info_block.get("openInterest"), minimum=0, optional=True)
                converted_rate = finite_number(rate * 100, optional=True) if rate is not None else None
                converted_oi = finite_number(oi * price, minimum=0) if oi is not None else None
                funding_rate_pct = round(converted_rate, 4) if converted_rate is not None else None
                open_interest_usd = round(converted_oi, 2) if converted_oi is not None else None
                has_perpetuals = funding_rate_pct is not None
            except Exception as fe:
                logger.info("Kraken Futures perpetual contract unavailable")

            # 4. Explicitly labeled 24h momentum bucket
            result = {
                "symbol": sym,
                "pair": pair,
                "price": price,
                "tick_size": tick_size,
                "status": "ok",
                "source": "kraken",
                "change_24h_pct": round(change_24h, 2) if change_24h is not None else None,
                "high_24h": high_24h,
                "low_24h": low_24h,
                "volume_24h_usd": round(volume_24h, 0) if volume_24h is not None else None,
                "rsi_14": rsi,
                "funding_rate_pct": funding_rate_pct,
                "open_interest_usd": open_interest_usd,
                "has_perpetuals": has_perpetuals,
                "momentum_bucket": momentum_bucket(change_24h),
                "candles": candles,
                "source_timestamp": source_timestamp,
                "request_started_at": request_started_at,
                "freshness_basis": freshness_basis,
                "fetched_at": ticker_fetched_at,
                "valid_until": valid_until,
                "is_fallback": False
            }

            if not market_is_fresh(result) or not market_numbers_valid(result):
                raise ValueError("Expired or invalid market data")
            await market_cache.set(cache_key, result, ttl=min(60, valid_until - time.time()))
            return result

        except Exception as e:
            logger.warning("Market data unavailable for %s", pair)
            return {"symbol": sym, "status": "unavailable", "source": "kraken", "reason": "market_data_unavailable", "price": None, "candles": [], "is_fallback": False, "fetched_at": time.time()}
        finally:
            await exchange.close()
            await futures_exchange.close()


market_service = MarketService()
