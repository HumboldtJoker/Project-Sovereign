"""Macro regime detection — risk-on / risk-off composite.

The old pipeline used VIX level alone. This blends four independent reads:
  1. VIX level + 5-day momentum        (fear now, fear building?)
  2. 10y-2y Treasury spread (T10Y2Y)   (curve inversion = late cycle)
  3. High-yield OAS (BAMLH0A0HYM2)     (credit stress leads equities)
  4. SPY trend: price vs 50d vs 200d   (what the tape itself says)

Each component scores in [-1, +1]; the mean is the regime score.

    score >=  0.30  RISK_ON   exposure x1.00
    score >= -0.10  NEUTRAL   exposure x0.75
    score >= -0.50  RISK_OFF  exposure x0.40
    else            CRISIS    exposure x0.00  (no new positions)

Cached 6h in sovereign_results/macro_regime.json.

Usage:  python3 macro_regime.py
"""

import logging
from datetime import datetime, timedelta

from sovereign_config import (RESULTS_DIR, alpaca_keys, cached_daily, clamp,
                              fred_key, save_json)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("macro")

CACHE_FILE = RESULTS_DIR / "macro_regime.json"

REGIME_BANDS = [
    (0.30, "RISK_ON", 1.00),
    (-0.10, "NEUTRAL", 0.75),
    (-0.50, "RISK_OFF", 0.40),
    (-9.99, "CRISIS", 0.00),
]


from alpaca.data.enums import Adjustment as _Adjustment
_ADJ_ALL = _Adjustment.ALL  # split/dividend-adjusted bars


def _fred_series(fred, series_id: str, days: int = 120):
    try:
        s = fred.get_series(series_id, observation_start=(
            datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")).dropna()
        return s if len(s) else None
    except Exception as e:
        log.warning("FRED %s failed: %s", series_id, e)
        return None


def _score_vix(fred) -> tuple[float, str]:
    s = _fred_series(fred, "VIXCLS", 30)
    if s is None:
        return 0.0, "VIX unavailable"
    vix = float(s.iloc[-1])
    chg5 = vix - float(s.iloc[-6]) if len(s) > 5 else 0.0
    # 13 → +1, 20 → 0, 32+ → -1 ; rising VIX drags the score
    level_score = clamp((20 - vix) / 9)
    momo_penalty = clamp(-chg5 / 8, -0.5, 0.2)
    return clamp(level_score + momo_penalty), f"VIX {vix:.1f} ({chg5:+.1f} 5d)"


def _score_curve(fred) -> tuple[float, str]:
    s = _fred_series(fred, "T10Y2Y", 30)
    if s is None:
        return 0.0, "T10Y2Y unavailable"
    spread = float(s.iloc[-1])
    # +1.0% steep → +1 ; 0 → 0 ; -0.75% inverted → -1
    return clamp(spread / 1.0) if spread >= 0 else clamp(spread / 0.75), f"10y-2y {spread:+.2f}%"


def _score_credit(fred) -> tuple[float, str]:
    s = _fred_series(fred, "BAMLH0A0HYM2", 120)
    if s is None:
        return 0.0, "HY OAS unavailable"
    oas = float(s.iloc[-1])
    chg20 = oas - float(s.iloc[-21]) if len(s) > 20 else 0.0
    # 3% tight → +1 ; 4.5% → 0 ; 6%+ stressed → -1 ; widening is the leading tell
    level_score = clamp((4.5 - oas) / 1.5)
    widening_penalty = clamp(-chg20 / 0.75, -0.6, 0.2)
    return clamp(level_score + widening_penalty), f"HY OAS {oas:.2f}% ({chg20:+.2f} 20d)"


def _score_sector_dispersion() -> tuple[float, str]:
    """Sector dispersion — high dispersion = rotation regime.

    When VIX says calm but sectors are diverging, the regime detector
    needs to see that. Measures std of 5-day returns across sector ETFs.
    High dispersion with flat VIX is the July 2026 trap.

    Added 2026-07-28 after VIX-Nasdaq divergence masked sector rotation.
    """
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    sector_etfs = ["XLK", "XLF", "XLE", "XLV", "XLI", "XLP", "XLY", "XLU", "XLB", "XLRE"]
    key, secret, _ = alpaca_keys()
    client = StockHistoricalDataClient(key, secret)

    try:
        bars = client.get_stock_bars(StockBarsRequest(adjustment=_ADJ_ALL, 
            symbol_or_symbols=sector_etfs, timeframe=TimeFrame.Day,
            start=(datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d"),
            end=(datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")))

        returns_5d = []
        for etf in sector_etfs:
            if etf in bars and len(list(bars[etf])) >= 5:
                closes = [float(b.close) for b in bars[etf]]
                ret = (closes[-1] / closes[-5] - 1) * 100 if len(closes) >= 5 else 0
                returns_5d.append(ret)

        if len(returns_5d) < 5:
            return 0.0, "sector dispersion: insufficient data"

        import statistics
        disp = statistics.stdev(returns_5d)
        mean_ret = statistics.mean(returns_5d)

        # Low dispersion (<1.5%) = sectors moving together = normal
        # High dispersion (>3%) = active rotation = dangerous for momentum
        # Score: +0.5 at disp=0.5, 0 at disp=2.0, -1.0 at disp=4.0
        score = clamp((2.0 - disp) / 2.0, -1.0, 0.5)

        return score, f"sector disp {disp:.1f}% (mean {mean_ret:+.1f}%), {len(returns_5d)} sectors"

    except Exception as e:
        log.warning("Sector dispersion failed: %s", e)
        return 0.0, "sector dispersion unavailable"


def _score_trend() -> tuple[float, str]:
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    key, secret, _ = alpaca_keys()
    client = StockHistoricalDataClient(key, secret)
    try:
        bars = client.get_stock_bars(StockBarsRequest(adjustment=_ADJ_ALL, 
            symbol_or_symbols=["SPY"], timeframe=TimeFrame.Day,
            start=(datetime.now() - timedelta(days=320)).strftime("%Y-%m-%d"),
            end=(datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")))
        closes = [float(b.close) for b in bars["SPY"]]
    except Exception as e:
        log.warning("SPY bars failed: %s", e)
        return 0.0, "SPY trend unavailable"
    if len(closes) < 200:
        return 0.0, f"SPY trend: only {len(closes)} bars"
    px = closes[-1]
    sma50 = sum(closes[-50:]) / 50
    sma200 = sum(closes[-200:]) / 200
    score = 0.0
    score += 0.5 if px > sma50 else -0.5
    score += 0.5 if sma50 > sma200 else -0.5
    return score, f"SPY {px:.0f} vs 50d {sma50:.0f} / 200d {sma200:.0f}"


def get_regime(force_refresh: bool = False) -> dict:
    if not force_refresh:
        cached = cached_daily(CACHE_FILE, max_age_hours=6)
        if cached:
            return cached

    from fredapi import Fred
    try:
        fred = Fred(api_key=fred_key())
    except Exception as e:
        log.warning("FRED init failed: %s", e)
        fred = None

    components = {}
    if fred is not None:
        for name, fn in (("vix", _score_vix), ("curve", _score_curve), ("credit", _score_credit)):
            sc, detail = fn(fred)
            components[name] = {"score": round(sc, 3), "detail": detail}
    sc, detail = _score_trend()
    components["trend"] = {"score": round(sc, 3), "detail": detail}
    sc, detail = _score_sector_dispersion()
    components["sector_dispersion"] = {"score": round(sc, 3), "detail": detail}

    usable = [c["score"] for c in components.values() if "unavailable" not in c["detail"]]
    score = sum(usable) / len(usable) if usable else 0.0

    for threshold, regime, mult in REGIME_BANDS:
        if score >= threshold:
            break

    result = {
        "timestamp": datetime.now().isoformat(),
        "score": round(score, 3),
        "regime": regime,
        "exposure_multiplier": mult,
        "components": components,
    }
    save_json(CACHE_FILE, result)
    return result


if __name__ == "__main__":
    r = get_regime(force_refresh=True)
    print(f"\nMACRO REGIME: {r['regime']} (score {r['score']:+.2f}, "
          f"exposure x{r['exposure_multiplier']:.2f})")
    for name, c in r["components"].items():
        print(f"  {name:<7s} {c['score']:>+5.2f}  {c['detail']}")
