"""
Anomaly detection for the Sovereign.

Detects unsafe market conditions (price spikes, volume blowouts) and
portfolio drift so the autonomous agent can pause or re-evaluate before
acting on stale or distorted data.

All functions return structured dicts ready for the ``safety_checks``
field in agent_log.json.
"""

import logging
import math
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Optional yfinance for the convenience runner
try:
    import yfinance as yf
    HAS_YFINANCE = True
except ImportError:
    HAS_YFINANCE = False

# Optional numpy -- we fall back to pure-Python stats when absent
try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False


# ------------------------------------------------------------------
# Internal statistics helpers
# ------------------------------------------------------------------

def _mean(values: List[float]) -> float:
    """Arithmetic mean, safe for empty lists."""
    if not values:
        return 0.0
    return sum(values) / len(values)


def _std(values: List[float]) -> float:
    """Population standard deviation, pure-Python fallback."""
    if HAS_NUMPY:
        return float(np.std(values)) if values else 0.0
    n = len(values)
    if n < 2:
        return 0.0
    mu = _mean(values)
    return math.sqrt(sum((x - mu) ** 2 for x in values) / n)


# ------------------------------------------------------------------
# Price anomaly
# ------------------------------------------------------------------

def detect_price_anomaly(
    ticker: str,
    current_price: float,
    historical_prices: List[float],
    std_dev_threshold: float = 3.0,
) -> Dict:
    """
    Flag a price that deviates more than *std_dev_threshold* standard
    deviations from the historical mean.

    Args:
        ticker: Stock symbol.
        current_price: Most recent price observation.
        historical_prices: List of recent closing prices (e.g. last 60
            trading days).  Must contain at least 5 data points.
        std_dev_threshold: Number of standard deviations to trigger the
            anomaly flag (default 3.0).

    Returns:
        Structured dict with ``is_anomaly``, z-score, and human-readable
        detail.
    """
    result: Dict = {
        "check": "price_anomaly",
        "ticker": ticker,
        "current_price": current_price,
        "is_anomaly": False,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    if len(historical_prices) < 5:
        result["detail"] = (
            f"Insufficient history ({len(historical_prices)} points, need >= 5)"
        )
        result["skipped"] = True
        logger.debug("Price anomaly skipped for %s: not enough data", ticker)
        return result

    mu = _mean(historical_prices)
    sigma = _std(historical_prices)

    if sigma == 0:
        result["detail"] = "Zero variance in historical prices"
        result["z_score"] = 0.0
        return result

    z_score = (current_price - mu) / sigma

    result["mean"] = round(mu, 4)
    result["std_dev"] = round(sigma, 4)
    result["z_score"] = round(z_score, 4)
    result["threshold"] = std_dev_threshold
    result["data_points"] = len(historical_prices)

    if abs(z_score) > std_dev_threshold:
        direction = "above" if z_score > 0 else "below"
        result["is_anomaly"] = True
        result["detail"] = (
            f"{ticker} price ${current_price:.2f} is {abs(z_score):.1f} "
            f"std devs {direction} mean ${mu:.2f} "
            f"(threshold: {std_dev_threshold})"
        )
        logger.warning("PRICE ANOMALY: %s", result["detail"])
    else:
        result["detail"] = (
            f"{ticker} price ${current_price:.2f} within normal range "
            f"(z={z_score:+.2f}, threshold +/-{std_dev_threshold})"
        )
        logger.debug("Price check OK for %s (z=%.2f)", ticker, z_score)

    return result


# ------------------------------------------------------------------
# Volume anomaly
# ------------------------------------------------------------------

def detect_volume_anomaly(
    ticker: str,
    current_volume: int,
    avg_volume: int,
    multiplier_threshold: float = 3.0,
) -> Dict:
    """
    Flag when current volume exceeds *multiplier_threshold* times the
    average volume.

    Args:
        ticker: Stock symbol.
        current_volume: Volume in the most recent session (or intraday
            so far).
        avg_volume: Historical average daily volume.
        multiplier_threshold: Ratio above which the flag is raised
            (default 3.0x).

    Returns:
        Structured dict with ``is_anomaly`` and context.
    """
    result: Dict = {
        "check": "volume_anomaly",
        "ticker": ticker,
        "current_volume": current_volume,
        "avg_volume": avg_volume,
        "is_anomaly": False,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    if avg_volume <= 0:
        result["detail"] = "Average volume unavailable or zero"
        result["skipped"] = True
        return result

    ratio = current_volume / avg_volume
    result["volume_ratio"] = round(ratio, 2)
    result["threshold"] = multiplier_threshold

    if ratio > multiplier_threshold:
        result["is_anomaly"] = True
        result["detail"] = (
            f"{ticker} volume {current_volume:,} is {ratio:.1f}x the "
            f"average {avg_volume:,} (threshold: {multiplier_threshold}x)"
        )
        logger.warning("VOLUME ANOMALY: %s", result["detail"])
    else:
        result["detail"] = (
            f"{ticker} volume {current_volume:,} is {ratio:.1f}x average "
            f"-- within normal range"
        )
        logger.debug("Volume check OK for %s (%.1fx avg)", ticker, ratio)

    return result


# ------------------------------------------------------------------
# Portfolio drift
# ------------------------------------------------------------------

def detect_portfolio_drift(
    current_allocation: Dict[str, float],
    target_allocation: Dict[str, float],
    threshold: float = 0.10,
) -> Dict:
    """
    Flag positions whose weight has drifted more than *threshold* from
    target.

    Both allocation dicts map asset/sector name -> weight as a fraction
    (0.0-1.0).  Entries in one dict but missing from the other are
    treated as 0.0 weight.

    Args:
        current_allocation: Actual portfolio weights.
        target_allocation: Desired portfolio weights.
        threshold: Absolute drift that triggers a flag (default 0.10
            i.e. 10 percentage points).

    Returns:
        Structured dict listing drifted positions and max drift.
    """
    all_keys = set(current_allocation) | set(target_allocation)

    drifts: List[Dict] = []
    max_drift = 0.0

    for key in sorted(all_keys):
        current = current_allocation.get(key, 0.0)
        target = target_allocation.get(key, 0.0)
        drift = current - target
        abs_drift = abs(drift)

        if abs_drift > max_drift:
            max_drift = abs_drift

        if abs_drift > threshold:
            direction = "overweight" if drift > 0 else "underweight"
            drifts.append({
                "asset": key,
                "current_weight": round(current, 4),
                "target_weight": round(target, 4),
                "drift": round(drift, 4),
                "abs_drift": round(abs_drift, 4),
                "direction": direction,
            })

    is_anomaly = len(drifts) > 0

    result: Dict = {
        "check": "portfolio_drift",
        "is_anomaly": is_anomaly,
        "threshold": threshold,
        "max_drift": round(max_drift, 4),
        "drifted_positions": drifts,
        "total_assets_checked": len(all_keys),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    if is_anomaly:
        names = ", ".join(d["asset"] for d in drifts)
        result["detail"] = (
            f"{len(drifts)} position(s) drifted beyond "
            f"{threshold * 100:.0f}% threshold: {names}"
        )
        logger.warning("PORTFOLIO DRIFT: %s", result["detail"])
    else:
        result["detail"] = (
            f"All {len(all_keys)} positions within "
            f"{threshold * 100:.0f}% drift threshold "
            f"(max drift: {max_drift * 100:.1f}%)"
        )
        logger.debug("Portfolio drift check OK (max %.2f%%)",
                      max_drift * 100)

    return result


# ------------------------------------------------------------------
# Convenience: run all checks for a single ticker using yfinance
# ------------------------------------------------------------------

def run_all_checks(
    ticker: str,
    price_history_days: int = 60,
    current_allocation: Optional[Dict[str, float]] = None,
    target_allocation: Optional[Dict[str, float]] = None,
    drift_threshold: float = 0.10,
) -> Dict:
    """
    Fetch live data from yfinance and run all anomaly detectors for
    *ticker*.

    Args:
        ticker: Stock symbol.
        price_history_days: Number of calendar days of closing prices
            to use for the price anomaly check (default 60).
        current_allocation: Optional portfolio allocation dict.  When
            provided alongside *target_allocation*, portfolio drift is
            also checked.
        target_allocation: Optional target allocation dict.
        drift_threshold: Drift threshold passed through to
            :func:`detect_portfolio_drift`.

    Returns:
        Combined results dict with per-check entries and an overall
        ``has_anomaly`` flag.
    """
    results: Dict = {
        "ticker": ticker,
        "checks": {},
        "has_anomaly": False,
        "errors": [],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    if not HAS_YFINANCE:
        results["errors"].append(
            "yfinance not installed -- cannot run live checks"
        )
        logger.error("run_all_checks: yfinance not available")
        return results

    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period=f"{price_history_days}d")

        if hist.empty:
            results["errors"].append(
                f"No historical data returned for {ticker}"
            )
            return results

        closes = hist["Close"].dropna().tolist()
        current_price = closes[-1] if closes else 0.0

        # --- Price anomaly ---
        # Use all closes except the last one as "historical" reference
        historical_prices = closes[:-1] if len(closes) > 1 else []
        price_result = detect_price_anomaly(
            ticker, current_price, historical_prices
        )
        results["checks"]["price_anomaly"] = price_result
        if price_result.get("is_anomaly"):
            results["has_anomaly"] = True

        # --- Volume anomaly ---
        volumes = hist["Volume"].dropna().tolist()
        if len(volumes) >= 2:
            current_vol = int(volumes[-1])
            avg_vol = int(_mean(volumes[:-1]))
            volume_result = detect_volume_anomaly(ticker, current_vol, avg_vol)
        else:
            volume_result = {
                "check": "volume_anomaly",
                "ticker": ticker,
                "skipped": True,
                "detail": "Insufficient volume data",
                "is_anomaly": False,
            }
        results["checks"]["volume_anomaly"] = volume_result
        if volume_result.get("is_anomaly"):
            results["has_anomaly"] = True

    except Exception as exc:
        msg = f"yfinance fetch failed for {ticker}: {exc}"
        results["errors"].append(msg)
        logger.error(msg)

    # --- Portfolio drift (only if allocations provided) ---
    if current_allocation is not None and target_allocation is not None:
        drift_result = detect_portfolio_drift(
            current_allocation, target_allocation, drift_threshold
        )
        results["checks"]["portfolio_drift"] = drift_result
        if drift_result.get("is_anomaly"):
            results["has_anomaly"] = True

    return results
