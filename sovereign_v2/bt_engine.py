"""Backtest engine — replays tagged signal events against historical bars.

Generalizes the ad hoc congress/momentum simulator in sovereign_backtest.py:
any signal source (congress herd, momentum, composite, future sources) feeds
in as a stream of SignalEvent and gets the same honest treatment —
chronological portfolio walk, live exit rules, a shared max-concurrent book.
Every trade keeps its originating signal_type so bt_metrics can break results
down by source instead of only in aggregate.

Exit rules mirror risk_engine.py: -stop_pct stop, +target_pct target, a
trailing-to-breakeven rule once a position is up trail_trigger_pct, or a
max_hold_days time exit — whichever comes first.
"""

from dataclasses import dataclass, field
from datetime import datetime

from sovereign_config import RISK


@dataclass
class SignalEvent:
    ticker: str
    date: object          # datetime.date the signal fired (public-knowledge date)
    signal_type: str      # "congress" | "momentum" | "composite" | ...
    conviction: str = "medium"   # high | medium | low — drives position size
    meta: dict = field(default_factory=dict)


DEFAULT_PARAMS = {
    "start_equity": 100.0,
    "stop_pct": RISK["stop_loss_pct"],
    "target_pct": RISK["target_pct"],
    "max_hold_days": 30,
    "max_concurrent": RISK["max_positions"],
    "signal_cooldown_days": 45,
    "trail_trigger_pct": RISK["trail_trigger_pct"],   # set None to disable trailing
    "position_pct": {
        "high": RISK["high_conviction_pct"],
        "medium": RISK["medium_conviction_pct"],
        "low": RISK["low_conviction_pct"],
    },
}


def _first_bar_after(series, sig_date):
    return next((i for i, (d, _) in enumerate(series) if d > sig_date), None)


def _walk_exit(series, idx, params):
    """Walk forward from entry idx applying stop/target/trailing/time exit."""
    entry_date, entry = series[idx]
    stop = entry * (1 - params["stop_pct"])
    target = entry * (1 + params["target_pct"]) if params["target_pct"] else None
    trail_trigger = params.get("trail_trigger_pct")
    trail_armed = False
    max_hold = params["max_hold_days"]

    for j in range(idx + 1, min(idx + 1 + max_hold, len(series))):
        d, c = series[j]
        if trail_trigger and not trail_armed and c >= entry * (1 + trail_trigger):
            stop = max(stop, entry * 1.005)  # breakeven + slippage — mirrors risk_engine
            trail_armed = True
        if c <= stop:
            return entry_date, entry, d, c, ("trail_stop" if trail_armed else "stop")
        if target and c >= target:
            return entry_date, entry, d, c, "target"

    j = min(idx + max_hold, len(series) - 1)
    return entry_date, entry, series[j][0], series[j][1], "time"


def simulate(events: list, prices: dict, params: dict = None) -> dict:
    """Chronological portfolio walk over signal events. Returns trades + equity curve.

    events: list of SignalEvent, any order.
    prices: {ticker: [(date, close), ...]} ascending, e.g. bt_data.closes(bars).
    """
    p = {**DEFAULT_PARAMS, **(params or {})}
    if not events:
        return {"error": "no signal events", "trades": [], "executed": [], "equity_curve": []}

    # One signal per (ticker, signal_type) per cooldown window — mirrors the
    # live dedup rule so replayed history doesn't over-count a single event.
    last_signal = {}
    trades = []
    for ev in sorted(events, key=lambda e: e.date):
        key = (ev.ticker, ev.signal_type)
        if key in last_signal and (ev.date - last_signal[key]).days < p["signal_cooldown_days"]:
            continue
        last_signal[key] = ev.date

        series = prices.get(ev.ticker)
        if not series:
            continue
        idx = _first_bar_after(series, ev.date)
        if idx is None or idx >= len(series) - 1:
            continue

        entry_date, entry, exit_date, exit_price, why = _walk_exit(series, idx, p)
        trades.append({
            "ticker": ev.ticker, "signal_type": ev.signal_type, "conviction": ev.conviction,
            "signal_date": str(ev.date), "entry_date": str(entry_date), "entry": round(entry, 2),
            "exit_date": str(exit_date), "exit": round(exit_price, 2),
            "return": round(exit_price / entry - 1, 4), "exit_why": why,
            "meta": ev.meta,
        })

    if not trades:
        return {"error": "no executable trades", "trades": [], "executed": [], "equity_curve": []}

    trades.sort(key=lambda t: t["entry_date"])
    equity = p["start_equity"]
    open_until = []   # exit dates of currently-open slots
    taken = skipped = 0
    curve = [{"date": trades[0]["entry_date"], "equity": round(equity, 2)}]
    for tr in trades:
        open_until = [d for d in open_until if d > tr["entry_date"]]
        if len(open_until) >= p["max_concurrent"]:
            skipped += 1
            tr["taken"] = False
            continue
        pct = p["position_pct"].get(tr["conviction"], p["position_pct"]["medium"])
        tr["taken"] = True
        tr["position_pct"] = pct
        taken += 1
        pnl = equity * pct * tr["return"]
        equity += pnl
        open_until.append(tr["exit_date"])
        curve.append({"date": tr["exit_date"], "equity": round(equity, 2)})

    return {
        "trades": trades,
        "executed": [t for t in trades if t.get("taken")],
        "equity_curve": curve,
        "final_equity": round(equity, 2),
        "start_equity": p["start_equity"],
        "trades_taken": taken,
        "trades_skipped_full_book": skipped,
        "params": {k: v for k, v in p.items()},
    }
