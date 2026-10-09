import logging
import math
import time
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, Any, Optional
from typesafe_sdk import AsyncTypeSafeClient, Choice, Noul, Score
from app.core.config import settings
from app.core.freshness import market_is_fresh, within_validity
from app.core.market_validation import market_numbers_valid, momentum_bucket

logger = logging.getLogger(__name__)
ACTIONS = ("STRONG_BUY", "BUY", "HOLD", "TAKE_PROFIT", "SELL", "STRONG_SELL")


class TypeSafeService:
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or settings.TYPESAFE_API_KEY

    async def evaluate_decision(
        self,
        symbol: str,
        market_data: Dict[str, Any],
        social_stats: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Evaluate market microstructure + social sentiment state using TypeSafe Jev System One.
        """
        sym = symbol.upper().replace("$", "")
        if (market_data.get("status") != "ok" or market_data.get("source") != "kraken"
                or market_data.get("is_fallback") or not market_is_fresh(market_data)
                or not market_numbers_valid(market_data)
                or social_stats.get("sample_size", 0) <= 0):
            return None
        price = market_data.get("price")
        if not isinstance(price, (float, int)) or not math.isfinite(price) or price <= 0:
            return None
        try:
            self._trade_levels(price, market_data.get("tick_size"), "BUY")
            self._trade_levels(price, market_data.get("tick_size"), "SELL")
        except (ValueError, TypeError, ArithmeticError):
            logger.warning("TypeSafe category=levels_infeasible")
            return None
        funding_rate = market_data.get("funding_rate_pct")
        open_interest_usd = market_data.get("open_interest_usd")
        has_perpetuals = bool(market_data.get("has_perpetuals", False))
        rsi = market_data.get("rsi_14", 50.0)
        change_24h = market_data.get("change_24h_pct")
        sample_size = social_stats.get("sample_size", 0)

        # Build structured state
        state = {
            "asset": sym,
            "market": {
                "current_price": price,
                "change_24h_pct": change_24h,
                "momentum_bucket": momentum_bucket(change_24h),
                "rsi_14": rsi,
                "has_perpetuals": has_perpetuals,
                "funding_rate_pct": funding_rate,
                "open_interest_usd": open_interest_usd,
                "volume_24h_usd": market_data.get("volume_24h_usd")
            },
            "social_stats": {
                "sample_size": sample_size,
                "author_diversity_pct": social_stats.get("author_diversity_pct", 0),
                "total_likes": social_stats.get("total_likes", 0),
                "polarity_score": social_stats.get("polarity_score", 0),
                "sentiment_label": social_stats.get("sentiment_label", "Neutral")
            },
            "representative_tweets": social_stats.get("stratified_sample", [])
        }

        # Questions for TypeSafe System One
        questions = {
            "trade_action": Choice(
                instructions=(
                    "Given `market` data (RSI, price change momentum, open interest, and perpetuals funding rate if present) "
                    "and `social_stats` across `sample_size` tweets, what is the best immediate trading action for `asset`?"
                ),
                criteria={
                    "STRONG_BUY": "High-conviction long (e.g. short squeeze setup, capitulation bottom, or major verified breakout).",
                    "BUY": "Favorable risk-to-reward long entry with positive upside expectation.",
                    "HOLD": "Neutral, range-bound, or consolidating; no clear asymmetric statistical edge.",
                    "TAKE_PROFIT": "Market is overbought or meeting heavy resistance; secure existing gains.",
                    "SELL": "Bearish breakdown, deteriorating momentum, or high downside continuation risk.",
                    "STRONG_SELL": "Crowded top exhaustion, extreme positive funding, or severe fundamental catalyst breakdown."
                }
            ),
            "sentiment_spectrum": Score(
                instructions="Rate the prevailing social mood in `social_stats` and `representative_tweets`.",
                criteria=[
                    "Extreme Panic / Capitulation",
                    "Cautious / Bearish",
                    "Neutral / Mixed",
                    "Optimistic / Bullish",
                    "Euphoric / Greedy"
                ]
            ),
            "is_short_squeeze_risk": Noul(
                instructions=(
                    "If `market.has_perpetuals` is true, does the state show negative `market.funding_rate_pct` clashing with "
                    "`social_stats.sentiment_label` panic at support, indicating a short squeeze risk? If perpetuals data is unavailable, "
                    "does extreme oversold RSI clashing with peak panic indicate a potential capitulation bounce?"
                )
            ),
            "catalyst_impact": Score(
                instructions="Rate the significance of any events or breaking news described in `representative_tweets`.",
                criteria=[
                    "No news or pure retail noise",
                    "Minor routine update or rumors",
                    "Moderate ecosystem milestone",
                    "Major market-shifting catalyst"
                ]
            )
        }

        # Check if API key is provided
        if self.api_key and self.api_key.strip() != "":
            try:
                async with AsyncTypeSafeClient(api_key=self.api_key) as client:
                    response = await client.system_one(state=state, questions=questions)

                action_ans = response.answers.get("trade_action")
                sentiment_ans = response.answers.get("sentiment_spectrum")
                squeeze_ans = response.answers.get("is_short_squeeze_risk")
                catalyst_ans = response.answers.get("catalyst_impact")

                trade_action = action_ans.choice
                action_conf = self._number(action_ans.confidence, 0, 1) * 100
                raw_probs = action_ans.probabilities
                action_probabilities = None
                if raw_probs:
                    if set(raw_probs) != set(ACTIONS):
                        raise ValueError("Incomplete action distribution")
                    probabilities = {k: self._number(v, 0, 1) for k, v in raw_probs.items()}
                    if not math.isclose(sum(probabilities.values()), 1, abs_tol=1e-6):
                        raise ValueError("Invalid probability sum")
                    action_probabilities = {k: v * 100 for k, v in probabilities.items()}

                sentiment_score_val = self._number(sentiment_ans.score, 0, 4)
                sentiment_levels = [
                    "Extreme Panic / Capitulation",
                    "Cautious / Bearish",
                    "Neutral / Mixed",
                    "Optimistic / Bullish",
                    "Euphoric / Greedy"
                ]
                sentiment_idx = min(max(0, int(round(sentiment_score_val))), len(sentiment_levels) - 1)
                sentiment_text = sentiment_levels[sentiment_idx]

                squeeze_prob = self._number(squeeze_ans.noul, 0, 1) * 100
                catalyst_score_val = self._number(catalyst_ans.score, 0, 3)

                # Model latency must not renew input validity.
                if not market_is_fresh(market_data) or ("valid_until" in social_stats and not within_validity(social_stats)):
                    return None
                return self._build_decision_output(
                    symbol=sym,
                    price=price,
                    trade_action=trade_action,
                    confidence=action_conf,
                    action_probabilities=action_probabilities,
                    sentiment_text=sentiment_text,
                    sentiment_score=sentiment_score_val,
                    squeeze_prob=squeeze_prob,
                    catalyst_score=catalyst_score_val,
                    funding_rate=funding_rate,
                    rsi=rsi,
                    change_24h=change_24h,
                    tick_size=market_data.get("tick_size"),
                    is_mock=False
                )

            except Exception:
                logger.warning("TypeSafe evaluation unavailable or invalid")
        return None

    @staticmethod
    def _number(value, lower, upper):
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not lower <= value <= upper:
            raise ValueError("Invalid provider number")
        return float(value)

    @staticmethod
    def _trade_levels(price, tick_size, trade_action):
        levels = None
        if trade_action in ("BUY", "STRONG_BUY", "SELL", "STRONG_SELL"):
            if tick_size is None or not math.isfinite(tick_size) or tick_size <= 0:
                raise ValueError("Missing valid tick size")
            p, tick = Decimal(str(price)), Decimal(str(tick_size))
            def level(factor):
                return float(((p * Decimal(factor) / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP)) * tick)
            buy = "BUY" in trade_action
            factors = ("0.995", "1.002", "0.962", "1.045", "1.085") if buy else ("0.998", "1.005", "1.038", "0.955", "0.915")
            entry_min, entry_max, stop_loss, tp1, tp2 = map(level, factors)
            ordered = (0 < stop_loss < entry_min <= price <= entry_max < tp1 < tp2) if buy else (0 < tp2 < tp1 < entry_min <= price <= entry_max < stop_loss)
            if not ordered:
                raise ValueError("Tick size collapses or reverses trade levels")
            sl_pct, tp1_pct, tp2_pct = [(value / price - 1) * 100 for value in (stop_loss, tp1, tp2)]
            levels = {"entry_range": [entry_min, entry_max], "stop_loss": stop_loss,
                      "stop_loss_pct": sl_pct, "target_1": tp1, "target_1_pct": tp1_pct,
                      "target_2": tp2, "target_2_pct": tp2_pct,
                      "risk_reward_ratio": abs(tp2_pct / sl_pct), "tick_size": tick_size,
                      "method": "fixed_percentage_heuristic"}

        return levels

    def _build_decision_output(
        self,
        symbol: str,
        price: float,
        trade_action: str,
        confidence: float,
        sentiment_text: str,
        sentiment_score: float,
        squeeze_prob: float,
        catalyst_score: float,
        funding_rate: Optional[float],
        rsi: float,
        change_24h: float,
        action_probabilities: Optional[Dict[str, float]] = None,
        tick_size: Optional[float] = None,
        is_mock: bool = False
    ) -> Dict[str, Any]:
        """Formulates actionable trade levels, probability distribution, and rationale."""
        if is_mock or trade_action not in ACTIONS:
            raise ValueError("Invalid decision source/action")
        self._number(price, 0, float("inf"))
        self._number(confidence, 0, 100)
        if price <= 0:
            raise ValueError("Price must be positive")
        normalized_probs = action_probabilities
        if normalized_probs is not None:
            if set(normalized_probs) != set(ACTIONS):
                raise ValueError("Incomplete distribution")
            for value in normalized_probs.values():
                self._number(value, 0, 100)
            if not math.isclose(sum(normalized_probs.values()), 100, abs_tol=1e-4):
                raise ValueError("Invalid distribution sum")
        levels = self._trade_levels(price, tick_size, trade_action)

        # Build dynamic rationale
        reasons = []
        if squeeze_prob > 60:
            if funding_rate is not None and funding_rate < 0:
                reasons.append(f"Short squeeze probability is elevated ({squeeze_prob}%) due to negative perpetual funding ({funding_rate}%).")
            else:
                reasons.append(f"Capitulation reversal probability is elevated ({squeeze_prob}%) amidst extreme retail panic.")
        if rsi < 35:
            reasons.append(f"RSI-14 indicates oversold conditions ({rsi}).")
        elif rsi > 68:
            reasons.append(f"RSI-14 indicates overbought momentum ({rsi}).")

        if "Panic" in sentiment_text or "Fear" in sentiment_text:
            reasons.append(f"Social sentiment reflects prevailing fear/panic ({sentiment_text}), presenting contrarian absorption.")
        elif "Euphoric" in sentiment_text:
            reasons.append(f"Social sentiment shows high euphoria ({sentiment_text}), warranting cautious position sizing.")

        if catalyst_score >= 2.0:
            reasons.append("Significant ecosystem or market-moving catalyst detected in recent discussions.")

        if not reasons:
            reasons.append("Market momentum and social sentiment are balanced with no extreme divergence.")

        rationale = " ".join(reasons)

        return {
            "symbol": symbol,
            "action": trade_action,
            "confidence_pct": confidence,
            "selected_action_probability_pct": normalized_probs.get(trade_action) if normalized_probs else None,
            "action_probabilities": normalized_probs,
            "sentiment_label": sentiment_text,
            "sentiment_score": round(float(sentiment_score), 2),
            "squeeze_risk_pct": squeeze_prob,
            "catalyst_impact_score": round(float(catalyst_score), 2),
            "trade_levels": levels,
            "rationale": rationale,
            "is_mock": is_mock
        }


typesafe_service = TypeSafeService()
