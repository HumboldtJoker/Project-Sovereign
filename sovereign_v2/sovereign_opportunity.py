"""Sovereign Opportunity Scanner — find what's moving outside the watchlist.

Scans for unusual activity across a broad universe:
- Unusual volume (2x+ average)
- Big movers (5%+ daily change)
- Deeply oversold (RSI < 30)
- Breakouts (new 30-day highs on volume)

Runs every few hours during market hours. Surfaces candidates for the
main scan to pick up.

Usage:
    python3 sovereign_opportunity.py
"""

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("opportunity")

from sovereign_config import RESULTS_DIR, alpaca_keys

ALPACA_KEY, ALPACA_SECRET, _ = alpaca_keys()

BROAD_UNIVERSE = [
    "AAPL", "GOOGL", "META", "AMZN", "NVDA", "MSFT", "TSLA", "AMD", "AVGO", "TSM",
    "COIN", "PLTR", "GEV", "SNOW", "CRWD", "NET", "DDOG", "MDB", "PANW", "ZS",
    "SQ", "SHOP", "RBLX", "U", "ABNB", "UBER", "LYFT", "DASH", "PINS", "SNAP",
    "SOFI", "HOOD", "AFRM", "UPST", "MARA", "RIOT", "CLSK", "HUT",
    "LLY", "UNH", "JNJ", "PFE", "ABBV", "MRK", "BMY",
    "JPM", "GS", "BAC", "WFC", "MS", "C", "V", "MA",
    "XOM", "CVX", "COP", "OXY", "SLB",
    "BA", "LMT", "RTX", "GD", "NOC",
    "DIS", "NFLX", "CMCSA", "WBD",
]


from alpaca.data.enums import Adjustment as _Adjustment
_ADJ_ALL = _Adjustment.ALL  # split/dividend-adjusted bars


def scan_movers():
    """Find unusual activity across the broad universe."""
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest, StockSnapshotRequest
    from alpaca.data.timeframe import TimeFrame

    client = StockHistoricalDataClient(ALPACA_KEY, ALPACA_SECRET)

    opportunities = []
    batch_size = 20

    for i in range(0, len(BROAD_UNIVERSE), batch_size):
        batch = BROAD_UNIVERSE[i:i+batch_size]
        try:
            bars = client.get_stock_bars(StockBarsRequest(adjustment=_ADJ_ALL, 
                symbol_or_symbols=batch,
                timeframe=TimeFrame.Day,
                start=(datetime.now() - timedelta(days=35)).strftime("%Y-%m-%d"),
                end=(datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d"),
            ))

            for ticker in batch:
                try:
                    bar_list = bars[ticker]
                except (KeyError, TypeError):
                    continue

                if len(bar_list) < 5:
                    continue

                last = bar_list[-1]
                prev = bar_list[-2]
                avg_vol = sum(b.volume for b in bar_list[-20:]) / min(len(bar_list), 20)

                daily_change = (last.close - prev.close) / prev.close * 100
                vol_ratio = last.volume / avg_vol if avg_vol > 0 else 0

                closes = [b.close for b in bar_list]
                rsi = compute_rsi(closes, 14)

                high_30d = max(b.high for b in bar_list[-20:])
                low_30d = min(b.low for b in bar_list[-20:])
                range_pos = (last.close - low_30d) / (high_30d - low_30d) if high_30d != low_30d else 0.5

                flags = []
                if vol_ratio >= 2.0:
                    flags.append(f"VOLUME {vol_ratio:.1f}x avg")
                if abs(daily_change) >= 5.0:
                    flags.append(f"MOVER {daily_change:+.1f}%")
                if rsi <= 30:
                    flags.append(f"OVERSOLD RSI={rsi:.0f}")
                if rsi >= 70:
                    flags.append(f"OVERBOUGHT RSI={rsi:.0f}")
                if range_pos > 0.95 and vol_ratio > 1.5:
                    flags.append("BREAKOUT (near 30d high + volume)")
                if range_pos < 0.05:
                    flags.append("BREAKDOWN (near 30d low)")

                if flags:
                    opportunities.append({
                        "ticker": ticker,
                        "price": float(last.close),
                        "daily_change": round(daily_change, 2),
                        "vol_ratio": round(vol_ratio, 2),
                        "rsi": round(rsi, 1),
                        "range_pos": round(range_pos, 2),
                        "flags": flags,
                    })

        except Exception as e:
            log.warning("Batch %d failed: %s", i, e)

    opportunities.sort(key=lambda x: len(x["flags"]), reverse=True)
    return opportunities


def compute_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0 for d in deltas[-period:]]
    losses = [-d if d < 0 else 0 for d in deltas[-period:]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def main():
    print(f"\n{'='*60}")
    print(f"SOVEREIGN OPPORTUNITY SCAN — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"Universe: {len(BROAD_UNIVERSE)} tickers")
    print(f"{'='*60}")

    opportunities = scan_movers()

    if not opportunities:
        print("\nNo unusual activity detected. Market is quiet.")
    else:
        print(f"\n{len(opportunities)} opportunities found:\n")
        for opp in opportunities:
            flags_str = " | ".join(opp["flags"])
            print(f"  {opp['ticker']:6s} ${opp['price']:>8.2f} "
                  f"Δ={opp['daily_change']:>+6.1f}% "
                  f"vol={opp['vol_ratio']:>4.1f}x "
                  f"RSI={opp['rsi']:>5.1f} "
                  f"rng={opp['range_pos']:.0%}")
            print(f"         {flags_str}")

    results_file = RESULTS_DIR / f"opportunity_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    results_file.write_text(json.dumps({
        "timestamp": datetime.now().isoformat(),
        "universe_size": len(BROAD_UNIVERSE),
        "opportunities": opportunities,
    }, indent=2))
    log.info("Saved to %s", results_file)

    if opportunities:
        new_tickers = [o["ticker"] for o in opportunities
                       if o["ticker"] not in ["AAPL", "GOOGL", "META", "AMZN", "NVDA", "MSFT",
                                               "GEV", "PLTR", "COIN", "AMD", "TSM", "AVGO"]]
        if new_tickers:
            print(f"\n💡 NEW tickers not on main watchlist: {', '.join(new_tickers)}")
            print(f"   Consider adding to sovereign_pipeline.py scan")


if __name__ == "__main__":
    main()
