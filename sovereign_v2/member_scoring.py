"""Congressional member track-record scoring.

Not all members trade equally well. This module computes, per member, the
forward excess return (vs SPY) of their past BUY disclosures, then converts
that into a signal weight used by herd detection: a 2-member herd of proven
performers now outranks a 4-member herd of noise traders.

Method:
  - For every buy with a parseable transaction date at least 30 calendar days
    old, measure the 30-trading-day forward return from the first close on or
    after the transaction date, minus SPY's return over the same span.
  - Member score = mean excess return, shrunk toward 0 by n/(n+3) so a member
    with 2 lucky trades doesn't dominate one with 40.
  - Weight = clamp(1 + shrunk_score * 8, 0.25, 3.0). 1.0 = average/unknown.

Usage:
    python3 member_scoring.py build      # compute scores from transactions.json
    python3 member_scoring.py show       # leaderboard
"""

import logging
import sys
from collections import defaultdict
from datetime import datetime, timedelta

from sovereign_config import DATA_DIR, alpaca_keys, load_json, save_json

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("member_scoring")

SCORES_FILE = DATA_DIR / "member_scores.json"
HOLD_TRADING_DAYS = 30
MIN_DATE = datetime(2022, 1, 1)


from alpaca.data.enums import Adjustment as _Adjustment
_ADJ_ALL = _Adjustment.ALL  # split/dividend-adjusted bars


def _parse_date(s: str):
    try:
        d = datetime.strptime(s, "%m/%d/%Y")
    except (ValueError, TypeError):
        return None
    # Filings sometimes OCR to future/ancient dates — discard garbage.
    if d < MIN_DATE or d > datetime.now():
        return None
    return d


def _fetch_bars(tickers: list[str], start: datetime) -> dict[str, list]:
    """Daily closes per ticker: [(date, close), ...] sorted ascending."""
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    key, secret, _ = alpaca_keys()
    client = StockHistoricalDataClient(key, secret)
    out = {}
    batch = 50
    symbols = sorted(set(tickers))
    for i in range(0, len(symbols), batch):
        chunk = symbols[i:i + batch]
        try:
            bars = client.get_stock_bars(StockBarsRequest(adjustment=_ADJ_ALL, 
                symbol_or_symbols=chunk,
                timeframe=TimeFrame.Day,
                start=start.strftime("%Y-%m-%d"),
                end=(datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d"),
            ))
            for sym in chunk:
                try:
                    out[sym] = [(b.timestamp.date(), float(b.close)) for b in bars[sym]]
                except (KeyError, TypeError):
                    continue
        except Exception as e:
            log.warning("Bar batch failed (%s...): %s", chunk[0], e)
    return out


def _forward_return(series: list, entry_date, hold_days: int):
    """Return over `hold_days` trading days from first close on/after entry_date."""
    idx = next((i for i, (d, _) in enumerate(series) if d >= entry_date.date()), None)
    if idx is None:
        return None
    exit_idx = min(idx + hold_days, len(series) - 1)
    if exit_idx <= idx:
        return None
    entry, exit_ = series[idx][1], series[exit_idx][1]
    if entry <= 0:
        return None
    return exit_ / entry - 1


def build_scores() -> dict:
    txs = load_json(DATA_DIR / "transactions.json", [])
    cutoff = datetime.now() - timedelta(days=45)  # need ~30 trading days of forward data

    scorable = []
    for tx in txs:
        if tx.get("direction") != "buy" or not tx.get("ticker"):
            continue
        d = _parse_date(tx.get("transaction_date", "")) or _parse_date(tx.get("filing_date", ""))
        if d is None or d > cutoff:
            continue
        scorable.append((tx["member"], tx["ticker"], d))

    if not scorable:
        log.warning("No scorable historical buys found.")
        return {}

    log.info("Scoring %d historical buys across %d members",
             len(scorable), len({m for m, _, _ in scorable}))

    earliest = min(d for _, _, d in scorable) - timedelta(days=5)
    tickers = [t for _, t, _ in scorable] + ["SPY"]
    prices = _fetch_bars(tickers, earliest)
    spy = prices.get("SPY", [])
    if not spy:
        log.error("No SPY data — cannot compute excess returns.")
        return {}

    per_member = defaultdict(list)
    for member, ticker, d in scorable:
        series = prices.get(ticker)
        if not series:
            continue
        r = _forward_return(series, d, HOLD_TRADING_DAYS)
        r_spy = _forward_return(spy, d, HOLD_TRADING_DAYS)
        if r is None or r_spy is None:
            continue
        per_member[member].append({"ticker": ticker, "date": str(d.date()),
                                   "fwd_30td": round(r, 4), "excess": round(r - r_spy, 4)})

    scores = {}
    for member, trades in per_member.items():
        n = len(trades)
        mean_excess = sum(t["excess"] for t in trades) / n
        hit_rate = sum(1 for t in trades if t["excess"] > 0) / n
        shrunk = mean_excess * n / (n + 3)
        weight = max(0.25, min(3.0, 1 + shrunk * 8))
        scores[member] = {
            "n_trades": n,
            "mean_excess_30td": round(mean_excess, 4),
            "hit_rate": round(hit_rate, 3),
            "weight": round(weight, 3),
            "trades": trades,
        }

    save_json(SCORES_FILE, {"built": datetime.now().isoformat(),
                            "hold_trading_days": HOLD_TRADING_DAYS,
                            "members": scores})
    log.info("Saved scores for %d members to %s", len(scores), SCORES_FILE)
    return scores


def load_member_weights() -> dict[str, float]:
    """member (lowercased) → weight. Empty dict if scores never built."""
    data = load_json(SCORES_FILE, {})
    return {m.lower().strip(): v["weight"] for m, v in data.get("members", {}).items()}


def show():
    data = load_json(SCORES_FILE, {})
    members = data.get("members", {})
    if not members:
        print("No scores built yet. Run: python3 member_scoring.py build")
        return
    print(f"\nCONGRESSIONAL TRACK RECORDS (built {data.get('built', '?')[:16]})")
    print(f"{'Member':<32s} {'N':>3s} {'Excess30':>9s} {'Hit%':>6s} {'Weight':>7s}")
    ranked = sorted(members.items(), key=lambda kv: kv[1]["weight"], reverse=True)
    for m, v in ranked:
        print(f"{m[:31]:<32s} {v['n_trades']:>3d} {v['mean_excess_30td']:>+8.1%} "
              f"{v['hit_rate']:>5.0%} {v['weight']:>7.2f}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "show"
    if cmd == "build":
        build_scores()
        show()
    else:
        show()
