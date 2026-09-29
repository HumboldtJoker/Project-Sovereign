"""Sovereign backtest — does the congressional herd edge actually pay?

Honest constraints:
  - Entries key off FILING dates (public knowledge), never transaction dates.
    Members file up to 45 days late; the backtest only knows what the scraper
    would have known.
  - Signal: >=2 members buying the same ticker within a 30-day window
    (same rule as live herd detection). One signal per ticker per 45 days.
  - Exit: -8% stop, +15% target, or 30 trading days, checked on closes. This
    is NOT the live exit logic: live uses thesis stops floored at 2x ATR / 8%,
    a one-time breakeven move at +10%, and no time exit. It also tests the
    congress signal alone, not the live composite. Treat its results as a
    measure of the congressional signal, not of the running strategy.
  - Sizing: equal-weight 20% of equity, max 5 concurrent, $100 start.
  - Benchmark: SPY buy-and-hold over the same span.

Also runs a `momentum` baseline (RSI<30 entries on the core watchlist) so the
congressional edge can be compared against a dumb-but-honest alternative.

Usage:
    python3 sovereign_backtest.py congress
    python3 sovereign_backtest.py momentum
"""

import logging
import sys
from collections import defaultdict
from datetime import datetime, timedelta

from sovereign_config import DATA_DIR, RESULTS_DIR, alpaca_keys, load_json, save_json

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("backtest")

START_EQUITY = 100.0
POSITION_PCT = 0.20
MAX_CONCURRENT = 5
STOP_PCT = 0.08
TARGET_PCT = 0.15
MAX_HOLD_TD = 30
HERD_WINDOW_DAYS = 30
HERD_MIN_MEMBERS = 2
SIGNAL_COOLDOWN_DAYS = 45


from alpaca.data.enums import Adjustment as _Adjustment
_ADJ_ALL = _Adjustment.ALL  # split/dividend-adjusted bars


def _parse(s: str):
    try:
        d = datetime.strptime(s, "%m/%d/%Y")
        return d if datetime(2022, 1, 1) <= d <= datetime.now() else None
    except (ValueError, TypeError):
        return None


def _bars(symbols: list[str], start: datetime) -> dict[str, list]:
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    key, secret, _ = alpaca_keys()
    client = StockHistoricalDataClient(key, secret)
    out = {}
    symbols = sorted(set(symbols))
    for i in range(0, len(symbols), 50):
        chunk = symbols[i:i + 50]
        try:
            bars = client.get_stock_bars(StockBarsRequest(adjustment=_ADJ_ALL, 
                symbol_or_symbols=chunk, timeframe=TimeFrame.Day,
                start=start.strftime("%Y-%m-%d"),
                end=(datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")))
            for sym in chunk:
                try:
                    out[sym] = [(b.timestamp.date(), float(b.close)) for b in bars[sym]]
                except (KeyError, TypeError):
                    continue
        except Exception as e:
            log.warning("Bars failed (%s...): %s", chunk[0], e)
    return out


def _congress_signal_events() -> list[dict]:
    """Replay filings chronologically; emit herd events as they'd have appeared."""
    txs = load_json(DATA_DIR / "transactions.json", [])
    buys = []
    for tx in txs:
        if tx.get("direction") != "buy" or not tx.get("ticker"):
            continue
        fd = _parse(tx.get("filing_date", ""))
        if fd is None:
            continue
        buys.append({"ticker": tx["ticker"], "member": tx["member"].lower().strip(),
                     "filed": fd})
    buys.sort(key=lambda b: b["filed"])

    window = defaultdict(list)   # ticker → [(member, filed)]
    last_signal = {}
    events = []
    for b in buys:
        t, d = b["ticker"], b["filed"]
        window[t] = [(m, fd) for m, fd in window[t] if (d - fd).days <= HERD_WINDOW_DAYS]
        window[t].append((b["member"], d))
        members = {m for m, _ in window[t]}
        if len(members) >= HERD_MIN_MEMBERS:
            if t in last_signal and (d - last_signal[t]).days < SIGNAL_COOLDOWN_DAYS:
                continue
            last_signal[t] = d
            events.append({"ticker": t, "date": d, "members": sorted(members)})
    return events


def _momentum_signal_events(prices: dict) -> list[dict]:
    events = []
    for sym, series in prices.items():
        if sym == "SPY":
            continue
        closes = [c for _, c in series]
        last_signal = None
        for i in range(20, len(series)):
            window = closes[max(0, i - 14):i + 1]
            deltas = [window[j] - window[j - 1] for j in range(1, len(window))]
            gains = sum(d for d in deltas if d > 0) / 14
            losses = sum(-d for d in deltas if d < 0) / 14
            rsi = 100.0 if losses == 0 else 100 - 100 / (1 + gains / losses)
            if rsi <= 30:
                d = series[i][0]
                if last_signal and (d - last_signal).days < SIGNAL_COOLDOWN_DAYS:
                    continue
                last_signal = d
                events.append({"ticker": sym, "date": datetime.combine(d, datetime.min.time()),
                               "members": []})
    events.sort(key=lambda e: e["date"])
    return events


def _simulate(events: list[dict], prices: dict) -> dict:
    """Chronological portfolio sim over signal events with live exit rules."""
    if not events:
        return {"error": "no signal events"}

    # Trade list first (each event independent), then portfolio equity curve.
    trades = []
    for ev in events:
        series = prices.get(ev["ticker"])
        if not series:
            continue
        sig_date = ev["date"].date() if isinstance(ev["date"], datetime) else ev["date"]
        idx = next((i for i, (d, _) in enumerate(series) if d > sig_date), None)
        if idx is None or idx >= len(series) - 1:
            continue
        entry_date, entry = series[idx]
        stop = entry * (1 - STOP_PCT)
        target = entry * (1 + TARGET_PCT)
        exit_price, exit_date, why = None, None, "time"
        for j in range(idx + 1, min(idx + 1 + MAX_HOLD_TD, len(series))):
            d, c = series[j]
            if c <= stop:
                exit_price, exit_date, why = c, d, "stop"
                break
            if c >= target:
                exit_price, exit_date, why = c, d, "target"
                break
        if exit_price is None:
            j = min(idx + MAX_HOLD_TD, len(series) - 1)
            exit_date, exit_price = series[j]
        trades.append({
            "ticker": ev["ticker"], "signal": str(sig_date),
            "entry_date": str(entry_date), "entry": round(entry, 2),
            "exit_date": str(exit_date), "exit": round(exit_price, 2),
            "return": round(exit_price / entry - 1, 4), "exit_why": why,
            "members": len(ev.get("members", [])),
        })

    if not trades:
        return {"error": "no executable trades"}

    # Portfolio walk: equal-weight slots, max concurrent
    trades.sort(key=lambda t: t["entry_date"])
    equity = START_EQUITY
    open_until = []  # exit dates of open slots
    taken = skipped = 0
    curve = []
    for tr in trades:
        open_until = [d for d in open_until if d > tr["entry_date"]]
        if len(open_until) >= MAX_CONCURRENT:
            skipped += 1
            tr["taken"] = False
            continue
        tr["taken"] = True
        taken += 1
        pnl = equity * POSITION_PCT * tr["return"]
        equity += pnl
        open_until.append(tr["exit_date"])
        curve.append({"date": tr["exit_date"], "equity": round(equity, 2)})

    executed = [t for t in trades if t.get("taken")]
    rets = [t["return"] for t in executed]
    wins = [r for r in rets if r > 0]

    spy = prices.get("SPY", [])
    spy_ret = None
    if spy and executed:
        first = executed[0]["entry_date"]
        s_idx = next((i for i, (d, _) in enumerate(spy) if str(d) >= first), 0)
        spy_ret = spy[-1][1] / spy[s_idx][1] - 1

    # max drawdown on the trade-resolution equity curve
    peak, max_dd = START_EQUITY, 0.0
    for pt in curve:
        peak = max(peak, pt["equity"])
        max_dd = max(max_dd, 1 - pt["equity"] / peak)

    return {
        "signals": len(events), "trades_taken": taken, "trades_skipped_full_book": skipped,
        "final_equity": round(equity, 2),
        "total_return": round(equity / START_EQUITY - 1, 4),
        "spy_return_same_span": round(spy_ret, 4) if spy_ret is not None else None,
        "win_rate": round(len(wins) / len(rets), 3) if rets else 0,
        "avg_win": round(sum(wins) / len(wins), 4) if wins else 0,
        "avg_loss": round(sum(r for r in rets if r <= 0) / max(len(rets) - len(wins), 1), 4),
        "max_drawdown": round(max_dd, 4),
        "exits": {k: sum(1 for t in executed if t["exit_why"] == k)
                  for k in ("stop", "target", "time")},
        "trades": executed,
    }


def run(mode: str):
    if mode == "congress":
        events = _congress_signal_events()
        if not events:
            print("No herd events reconstructable from transactions.json.")
            return
        tickers = sorted({e["ticker"] for e in events}) + ["SPY"]
        earliest = min(e["date"] for e in events) - timedelta(days=5)
        prices = _bars(tickers, earliest)
    else:
        core = ["AAPL", "GOOGL", "META", "AMZN", "NVDA", "MSFT",
                "GEV", "PLTR", "COIN", "AMD", "TSM", "AVGO", "SPY"]
        prices = _bars(core, datetime.now() - timedelta(days=540))
        events = _momentum_signal_events(prices)

    result = _simulate(events, prices)
    result["mode"] = mode
    result["run_at"] = datetime.now().isoformat()
    out = RESULTS_DIR / f"backtest_{mode}_{datetime.now().strftime('%Y%m%d')}.json"
    save_json(out, result)

    if "error" in result:
        print(f"Backtest failed: {result['error']}")
        return

    print(f"\n{'=' * 60}\nBACKTEST [{mode}] — filing-date-honest, live exit rules")
    print(f"{'=' * 60}")
    print(f"Signals: {result['signals']} | Taken: {result['trades_taken']} "
          f"(skipped {result['trades_skipped_full_book']}, book full)")
    print(f"$100 → ${result['final_equity']:.2f}  ({result['total_return']:+.1%})")
    if result["spy_return_same_span"] is not None:
        edge = result["total_return"] - result["spy_return_same_span"]
        print(f"SPY same span: {result['spy_return_same_span']:+.1%}  →  EDGE {edge:+.1%}")
    print(f"Win rate {result['win_rate']:.0%} | avg win {result['avg_win']:+.1%} "
          f"| avg loss {result['avg_loss']:+.1%} | max DD {result['max_drawdown']:.1%}")
    print(f"Exits: {result['exits']}")
    print(f"Saved: {out}")


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "congress")
