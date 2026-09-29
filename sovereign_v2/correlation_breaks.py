"""Correlation-break detection — when correlated assets diverge, something moved.

Watches historically-correlated pairs. When a pair whose 60-day daily-return
correlation is high suddenly diverges over 5 days by more than 2 standard
deviations of its usual 5-day spread, that's an event:

  - The LAGGARD is a mean-reversion candidate (small positive score), OR
  - the divergence is information (one name broke) — so it's surfaced as a
    flag for the thesis prompt rather than traded blindly.

Usage:  python3 correlation_breaks.py
"""

import logging
import math
from datetime import datetime, timedelta

from sovereign_config import RESULTS_DIR, alpaca_keys, cached_daily, save_json

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("corr_breaks")

CACHE_FILE = RESULTS_DIR / "corr_breaks.json"

PAIRS = [
    ("NVDA", "AMD"), ("V", "MA"), ("XOM", "CVX"), ("JPM", "GS"),
    ("GOOGL", "META"), ("MARA", "RIOT"), ("LMT", "NOC"), ("HOOD", "COIN"),
    ("CRWD", "PANW"), ("UBER", "LYFT"), ("AVGO", "TSM"), ("SPY", "QQQ"),
]

MIN_CORR = 0.55       # only pairs that actually co-move matter
Z_THRESHOLD = 2.0
LOOKBACK_DAYS = 90


from alpaca.data.enums import Adjustment as _Adjustment
_ADJ_ALL = _Adjustment.ALL  # split/dividend-adjusted bars


def _daily_closes(symbols: list[str]) -> dict[str, list[float]]:
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    key, secret, _ = alpaca_keys()
    client = StockHistoricalDataClient(key, secret)
    try:
        bars = client.get_stock_bars(StockBarsRequest(adjustment=_ADJ_ALL, 
            symbol_or_symbols=sorted(set(symbols)), timeframe=TimeFrame.Day,
            start=(datetime.now() - timedelta(days=LOOKBACK_DAYS + 40)).strftime("%Y-%m-%d"),
            end=(datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")))
    except Exception as e:
        log.error("Bars failed: %s", e)
        return {}
    out = {}
    for sym in set(symbols):
        try:
            out[sym] = [float(b.close) for b in bars[sym]]
        except (KeyError, TypeError):
            continue
    return out


def _returns(closes: list[float]) -> list[float]:
    return [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]


def _corr(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b))
    a, b = a[-n:], b[-n:]
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va <= 0 or vb <= 0:
        return 0.0
    return cov / math.sqrt(va * vb)


def detect_breaks(force_refresh: bool = False) -> dict:
    if not force_refresh:
        cached = cached_daily(CACHE_FILE, max_age_hours=6)
        if cached:
            return cached

    symbols = [s for pair in PAIRS for s in pair]
    closes = _daily_closes(symbols)

    events = []
    for a, b in PAIRS:
        ca, cb = closes.get(a), closes.get(b)
        if not ca or not cb or min(len(ca), len(cb)) < 40:
            continue
        n = min(len(ca), len(cb))
        ca, cb = ca[-n:], cb[-n:]
        ra, rb = _returns(ca), _returns(cb)

        corr = _corr(ra[:-5], rb[:-5])
        if corr < MIN_CORR:
            continue

        # 5-day relative-performance spreads across history → how unusual is now?
        spreads = []
        for i in range(5, len(ca)):
            pa = ca[i] / ca[i - 5] - 1
            pb = cb[i] / cb[i - 5] - 1
            spreads.append(pa - pb)
        if len(spreads) < 20:
            continue
        hist = spreads[:-1]
        mean = sum(hist) / len(hist)
        var = sum((s - mean) ** 2 for s in hist) / len(hist)
        std = math.sqrt(var) if var > 0 else 1e-9
        z = (spreads[-1] - mean) / std

        if abs(z) >= Z_THRESHOLD:
            leader, laggard = (a, b) if spreads[-1] > 0 else (b, a)
            events.append({
                "pair": f"{a}/{b}",
                "corr_60d": round(corr, 2),
                "spread_5d": round(spreads[-1], 4),
                "z": round(z, 2),
                "leader": leader,
                "laggard": laggard,
                "note": f"{leader} outran {laggard} by {abs(spreads[-1]):.1%} in 5d "
                        f"(z={z:+.1f}, corr {corr:.2f})",
            })

    result = {"timestamp": datetime.now().isoformat(), "events": events,
              "pairs_checked": len(PAIRS)}
    save_json(CACHE_FILE, result)
    return result


def score_for_ticker(ticker: str, breaks: dict) -> tuple[float, str]:
    """Laggard in a broken high-corr pair gets a modest catch-up score."""
    for ev in breaks.get("events", []):
        if ev["laggard"] == ticker:
            strength = min(abs(ev["z"]) / 4.0, 1.0)
            return round(0.5 * strength, 3), ev["note"]
        if ev["leader"] == ticker:
            return 0.0, ev["note"]  # informational only — momentum vs stretch is ambiguous
    return 0.0, ""


if __name__ == "__main__":
    r = detect_breaks(force_refresh=True)
    print(f"\nCORRELATION BREAKS — {r['pairs_checked']} pairs checked")
    if not r["events"]:
        print("  No divergences beyond 2σ. Pairs are behaving.")
    for ev in r["events"]:
        print(f"  ⚡ {ev['note']}")
