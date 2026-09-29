"""Earnings radar — earnings proximity + pre-earnings whisper detection.

Two jobs:
  1. RISK FLAG: earnings within the stop-loss horizon is binary-event risk a
     $100 account cannot diversify away. Any ticker reporting within
     EARNINGS_RISK_DAYS gets flagged; the risk engine halves size and the
     thesis prompt is told explicitly.
  2. WHISPER: news velocity (3-day article count vs trailing 30-day daily
     average, via Alpaca News API) plus headline tone gives a crude read on
     which way the pre-earnings wind blows.

Usage:  python3 earnings_radar.py NVDA AAPL ...
"""

import logging
import sys
from datetime import datetime, timedelta, timezone

from sovereign_config import RESULTS_DIR, alpaca_keys, clamp, load_json, save_json

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("earnings")

CACHE_FILE = RESULTS_DIR / "earnings_radar_cache.json"
EARNINGS_RISK_DAYS = 7

POSITIVE_WORDS = ("beat", "beats", "raise", "raises", "upgrade", "upgrades", "record",
                  "surge", "strong", "tops", "exceeds", "outperform", "bullish", "rally")
NEGATIVE_WORDS = ("miss", "misses", "cut", "cuts", "downgrade", "downgrades", "warns",
                  "warning", "weak", "slump", "lawsuit", "probe", "recall", "bearish", "plunge")


def _next_earnings(sym: str):
    """Next earnings date via yfinance, or None."""
    import yfinance as yf
    t = yf.Ticker(sym)
    today = datetime.now().date()
    try:
        df = t.get_earnings_dates(limit=8)
        if df is not None and len(df):
            future = [d.date() for d in df.index if d.date() >= today]
            if future:
                return min(future)
    except Exception:
        pass
    try:
        cal = t.calendar
        dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
        if dates:
            future = [d for d in dates if d >= today]
            if future:
                return min(future)
    except Exception:
        pass
    return None


def _news_pulse(sym: str) -> dict:
    """Article velocity + headline tone from Alpaca News API."""
    try:
        from alpaca.data.historical.news import NewsClient
        from alpaca.data.requests import NewsRequest
    except ImportError:
        return {"velocity": 1.0, "tone": 0.0, "n_recent": 0}

    key, secret, _ = alpaca_keys()
    client = NewsClient(key, secret)
    now = datetime.now(timezone.utc)

    def _fetch(start, limit=50):
        try:
            resp = client.get_news(NewsRequest(symbols=sym, start=start, limit=limit))
            items = getattr(resp, "news", None)
            if items is None and hasattr(resp, "data"):
                items = resp.data.get("news", [])
            return list(items or [])
        except Exception as e:
            log.warning("News fetch failed for %s: %s", sym, e)
            return []

    recent = _fetch(now - timedelta(days=3))
    baseline = _fetch(now - timedelta(days=30))
    base_daily = max(len(baseline) / 30.0, 0.1)
    velocity = (len(recent) / 3.0) / base_daily

    tone_hits = 0
    tone_total = 0
    for item in recent:
        headline = (getattr(item, "headline", "") or "").lower()
        pos = sum(1 for w in POSITIVE_WORDS if w in headline)
        neg = sum(1 for w in NEGATIVE_WORDS if w in headline)
        if pos or neg:
            tone_total += 1
            tone_hits += 1 if pos > neg else -1 if neg > pos else 0
    tone = tone_hits / tone_total if tone_total else 0.0

    return {"velocity": round(velocity, 2), "tone": round(tone, 2), "n_recent": len(recent)}


def get_radar(tickers: list[str]) -> dict[str, dict]:
    today = datetime.now().strftime("%Y-%m-%d")
    cache = load_json(CACHE_FILE, {})
    if cache.get("date") != today:
        cache = {"date": today, "tickers": {}}

    out = {}
    for sym in tickers:
        if sym in cache["tickers"]:
            out[sym] = cache["tickers"][sym]
            continue
        log.info("Earnings radar: %s", sym)
        edate = _next_earnings(sym)
        days_to = (edate - datetime.now().date()).days if edate else None
        imminent = days_to is not None and 0 <= days_to <= EARNINGS_RISK_DAYS

        pulse = _news_pulse(sym)
        # Whisper only matters when earnings are near AND chatter is elevated.
        whisper = 0.0
        if imminent and pulse["velocity"] >= 1.5:
            whisper = clamp(pulse["tone"] * min((pulse["velocity"] - 1) / 2, 1.0))

        result = {
            "ticker": sym,
            "earnings_date": str(edate) if edate else None,
            "days_to_earnings": days_to,
            "earnings_imminent": imminent,
            "news_velocity": pulse["velocity"],
            "news_tone": pulse["tone"],
            "whisper_score": round(whisper, 3),
            "detail": (f"earnings {edate} ({days_to}d)" if edate else "no earnings date")
                      + f", news {pulse['velocity']:.1f}x tone {pulse['tone']:+.1f}",
        }
        cache["tickers"][sym] = result
        out[sym] = result

    save_json(CACHE_FILE, cache)
    return out


if __name__ == "__main__":
    syms = [s.upper() for s in sys.argv[1:]] or ["NVDA", "AAPL", "AMD"]
    radar = get_radar(syms)
    print(f"\nEARNINGS RADAR — {datetime.now().strftime('%Y-%m-%d')}")
    for sym, r in radar.items():
        flag = " ⚠️ IMMINENT" if r["earnings_imminent"] else ""
        print(f"  {sym:6s} whisper={r['whisper_score']:+.2f}  {r['detail']}{flag}")
