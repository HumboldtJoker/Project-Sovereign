"""Historical data fetcher — cached Alpaca daily bars.

Backtests re-run often (weekly cron, ad hoc research) and tend to re-request
the same symbol/date ranges. Every bar fetched here is cached to disk
(sovereign_state/bars_cache/<SYMBOL>.json) so only the missing edges of a
requested range are ever re-fetched from Alpaca.

Usage:
    from bt_data import get_bars, closes
    bars = get_bars(["AAPL", "SPY"], datetime(2024, 1, 1))
    series = closes(bars["SPY"])   # [(date, close), ...] ascending
"""

import logging
from datetime import datetime, timedelta

from sovereign_config import STATE_DIR, alpaca_keys, load_json, save_json

log = logging.getLogger("bt_data")

# Versioned by adjustment: caches written before split/dividend adjustment
# hold raw bars, and joining them to an adjusted tail turns a split into a
# crash. A corporate action inside a cached range still needs a refetch.
CACHE_DIR = STATE_DIR / "bars_cache_adj_all"
CACHE_DIR.mkdir(exist_ok=True)

DATE_FMT = "%Y-%m-%d"


from alpaca.data.enums import Adjustment as _Adjustment
_ADJ_ALL = _Adjustment.ALL  # split/dividend-adjusted bars


def _cache_path(symbol: str):
    return CACHE_DIR / f"{symbol.upper()}.json"


def _load_cache(symbol: str) -> dict:
    return load_json(_cache_path(symbol), {"bars": {}})


def _save_cache(symbol: str, cache: dict):
    save_json(_cache_path(symbol), cache)


def _fetch_alpaca(symbols: list, start: datetime, end: datetime) -> dict:
    """Raw Alpaca fetch, chunked to stay under the practical per-request symbol limit."""
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    key, secret, _ = alpaca_keys()
    client = StockHistoricalDataClient(key, secret)
    out = {s: [] for s in symbols}
    symbols = sorted(set(symbols))
    for i in range(0, len(symbols), 50):
        chunk = symbols[i:i + 50]
        try:
            bars = client.get_stock_bars(StockBarsRequest(adjustment=_ADJ_ALL, 
                symbol_or_symbols=chunk, timeframe=TimeFrame.Day,
                start=start.strftime(DATE_FMT), end=end.strftime(DATE_FMT)))
            for sym in chunk:
                try:
                    out[sym] = [{
                        "date": b.timestamp.date().strftime(DATE_FMT),
                        "open": float(b.open), "high": float(b.high),
                        "low": float(b.low), "close": float(b.close),
                        "volume": int(b.volume),
                    } for b in bars[sym]]
                except (KeyError, TypeError):
                    continue
        except Exception as e:
            log.warning("Bars fetch failed (%s...): %s", chunk[0], e)
    return out


def get_bars(symbols: list, start: datetime, end: datetime = None) -> dict:
    """Daily OHLCV bars for each symbol over [start, end], cached on disk.

    Returns {symbol: [{"date","open","high","low","close","volume"}, ...]}
    sorted ascending by date. `end` defaults to yesterday (today's bar may
    still be forming intraday).
    """
    end = end or (datetime.now() - timedelta(days=1))
    symbols = sorted(set(s.upper() for s in symbols))

    caches = {sym: _load_cache(sym) for sym in symbols}
    to_fetch = {}
    for sym, cache in caches.items():
        have_dates = sorted(cache["bars"].keys())
        gaps = []
        if not have_dates:
            gaps.append((start, end))
        else:
            cached_start = datetime.strptime(have_dates[0], DATE_FMT)
            cached_end = datetime.strptime(have_dates[-1], DATE_FMT)
            if start < cached_start:
                gaps.append((start, cached_start - timedelta(days=1)))
            if end > cached_end:
                gaps.append((cached_end + timedelta(days=1), end))
        if gaps:
            to_fetch[sym] = gaps

    if to_fetch:
        earliest = min(g[0][0] for g in to_fetch.values())
        latest = max(g[-1][1] for g in to_fetch.values())
        log.info("Fetching %d symbol(s) from Alpaca (%s to %s)...",
                 len(to_fetch), earliest.strftime(DATE_FMT), latest.strftime(DATE_FMT))
        fetched = _fetch_alpaca(list(to_fetch.keys()), earliest, latest)
        for sym, bars in fetched.items():
            cache = caches[sym]
            for b in bars:
                cache["bars"][b["date"]] = b
            _save_cache(sym, cache)

    out = {}
    start_s, end_s = start.strftime(DATE_FMT), end.strftime(DATE_FMT)
    for sym in symbols:
        cache = caches[sym]
        out[sym] = [cache["bars"][d] for d in sorted(cache["bars"].keys())
                    if start_s <= d <= end_s]
    return out


def closes(bars: list) -> list:
    """[(date_obj, close), ...] ascending — the shape the engine consumes."""
    return [(datetime.strptime(b["date"], DATE_FMT).date(), b["close"]) for b in bars]
