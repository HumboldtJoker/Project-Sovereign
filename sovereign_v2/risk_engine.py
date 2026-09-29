"""Risk engine — position sizing, stops, correlation-aware book, circuit breaker.

Built for an account where every dollar matters. All limits are fractions of
live equity, so the same code protects $100 and $100K identically.

Sizing chain (multiplicative):
  base by conviction (25/15/10%) → x regime exposure multiplier
  → capped so position_pct x stop_pct <= max_risk_per_trade (2% equity)
  → halved if earnings imminent → halved if avg correlation with book > 0.70
  → sector cap 50%, position cap 25%, cash reserve 10%, min notional $1.

Stops on fractional shares: Alpaca doesn't support bracket orders on
fractional/notional orders, so stops are SYNTHETIC — recorded in
sovereign_state/positions_state.json and enforced by `sovereign_execute.py
manage` (cron, every 30 min in market hours). Breakeven rule: at +10%
unrealized, the stop rises ONCE to breakeven (+0.5%); at +target, exit. The
state key is called `trail_armed` for history's sake -- it does not trail.
Stops are floored at the rule (2x ATR, never closer than 8%): see
enforce_stop_rule().

Circuit breaker: equity down daily_loss_halt_pct (5%) vs yesterday's close
→ writes a halt file; execute/manage refuse new entries until the next day.
"""

import json
import logging
import math
from datetime import datetime, timedelta

from sovereign_config import RISK, STATE_DIR, alpaca_keys, load_json, save_json, with_timeout

log = logging.getLogger("risk")

POSITIONS_STATE = STATE_DIR / "positions_state.json"
HALT_FILE = STATE_DIR / "halt.json"


# --------------------------------------------------------------------------
# Circuit breaker
# --------------------------------------------------------------------------

from alpaca.data.enums import Adjustment as _Adjustment
_ADJ_ALL = _Adjustment.ALL  # split/dividend-adjusted bars


def check_circuit_breaker(account) -> bool:
    """True if trading is halted. Trips the halt on a >5% daily drawdown."""
    today = datetime.now().strftime("%Y-%m-%d")
    halt = load_json(HALT_FILE, {})
    if halt.get("date") == today:
        return True

    equity = float(account.equity)
    last_equity = float(account.last_equity or equity)
    if last_equity > 0 and (equity / last_equity - 1) <= -RISK["daily_loss_halt_pct"]:
        save_json(HALT_FILE, {
            "date": today,
            "reason": f"equity {equity:.2f} vs yesterday {last_equity:.2f} "
                      f"({equity / last_equity - 1:+.1%}) breached "
                      f"-{RISK['daily_loss_halt_pct']:.0%} daily limit",
            "tripped_at": datetime.now().isoformat(),
        })
        log.error("CIRCUIT BREAKER TRIPPED: %s", load_json(HALT_FILE)["reason"])
        return True
    return False


# --------------------------------------------------------------------------
# Consecutive-loss circuit breaker — reduce size after losing streaks
# --------------------------------------------------------------------------

TRADE_LOG = STATE_DIR / "trade_log.jsonl"


def get_consecutive_losses(trade_log_path=None) -> int:
    """Count consecutive losing exits from the most recent trade backward.

    A sell that carries `pnl_pct` (every exit logged since 2026-09-24) is a
    loss when pnl_pct < 0 and a streak-breaker otherwise. Older records have
    no prices, so they fall back to the original rule: 'why' containing STOP
    is a loss. That rule counted a breakeven stop -- a small WIN -- as a loss,
    which is how the 25% sizing cut could stick indefinitely.
    """
    path = trade_log_path or TRADE_LOG
    try:
        lines = path.read_text().strip().splitlines()
    except FileNotFoundError:
        return 0
    if not lines:
        return 0

    # Walk backward through sell actions only. A reconciled record with no
    # price says nothing about win or loss (the buy may never have filled),
    # so it neither counts nor breaks the streak; the same order id is only
    # counted once even if an old run logged it twice.
    consecutive, seen = 0, set()
    for line in reversed(lines):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("action") != "sell":
            continue
        oid = rec.get("order_id")
        if oid and oid in seen:
            continue
        seen.add(oid)
        if rec.get("source") == "reconciled" and rec.get("pnl_pct") is None:
            continue
        if rec.get("partial"):
            continue      # one trade exited in pieces counts once, at its final exit
        if rec.get("pnl_pct") is not None:
            if float(rec["pnl_pct"]) < 0:
                consecutive += 1
                continue
            break
        why = (rec.get("why") or "").upper()
        if "STOP" in why:
            consecutive += 1
        elif "TARGET" in why:
            # Winner — streak is broken
            break
        else:
            # Ambiguous exit (manual close, etc) — treat as streak-breaker
            break
    return consecutive


# --------------------------------------------------------------------------
# Correlation of a candidate vs current book
# --------------------------------------------------------------------------

def _return_series(symbols: list[str], days: int = 90) -> dict[str, list[float]]:
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    key, secret, _ = alpaca_keys()
    client = with_timeout(StockHistoricalDataClient(key, secret))
    try:
        bars = client.get_stock_bars(StockBarsRequest(adjustment=_ADJ_ALL, 
            symbol_or_symbols=sorted(set(symbols)), timeframe=TimeFrame.Day,
            start=(datetime.now() - timedelta(days=days + 20)).strftime("%Y-%m-%d"),
            end=(datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")))
    except Exception as e:
        log.warning("Correlation bars failed: %s", e)
        return {}
    out = {}
    for sym in set(symbols):
        try:
            closes = [float(b.close) for b in bars[sym]]
            out[sym] = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
        except (KeyError, TypeError):
            continue
    return out


def _corr(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b))
    if n < 20:
        return 0.0
    a, b = a[-n:], b[-n:]
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    return cov / math.sqrt(va * vb) if va > 0 and vb > 0 else 0.0


def avg_correlation_with_book(candidate: str, held_symbols: list[str]) -> float:
    if not held_symbols:
        return 0.0
    series = _return_series([candidate] + held_symbols)
    cand = series.get(candidate)
    if not cand:
        return 0.0
    corrs = [_corr(cand, series[h]) for h in held_symbols if h in series]
    return sum(corrs) / len(corrs) if corrs else 0.0


# --------------------------------------------------------------------------
# Position sizing
# --------------------------------------------------------------------------

def size_position(equity: float, cash: float, conviction: str,
                  regime_multiplier: float, positions: list, sector_map: dict,
                  candidate: str, stop_pct: float = None,
                  earnings_imminent: bool = False) -> dict:
    """Returns {"notional": $, "pct": frac, "reasons": [...]} — notional 0 = rejected."""
    stop_pct = stop_pct or RISK["stop_loss_pct"]
    reasons = []

    base = {"high": RISK["high_conviction_pct"],
            "medium": RISK["medium_conviction_pct"],
            "low": RISK["low_conviction_pct"]}.get(conviction, 0.0)
    if base == 0.0:
        return {"notional": 0.0, "pct": 0.0, "reasons": ["conviction none"]}
    pct = base

    if regime_multiplier <= 0:
        return {"notional": 0.0, "pct": 0.0, "reasons": ["regime multiplier 0 — no new positions"]}
    pct *= regime_multiplier
    reasons.append(f"base {base:.0%} x regime {regime_multiplier:.2f}")

    risk_cap = RISK["max_risk_per_trade_pct"] / stop_pct
    if pct > risk_cap:
        pct = risk_cap
        reasons.append(f"risk-per-trade cap → {pct:.0%} ({stop_pct:.0%} stop)")

    if earnings_imminent:
        pct *= 0.5
        reasons.append("earnings imminent → halved")

    # Consecutive-loss circuit breaker: after 2+ consecutive stops, reduce
    # sizing to 25% of normal until a winner resets the counter.
    consec_losses = get_consecutive_losses()
    if consec_losses >= 2:
        pct *= 0.25
        reasons.append(f"Circuit breaker: {consec_losses} consecutive losses, sizing reduced to 25%")
        log.warning("Circuit breaker: %d consecutive losses, sizing reduced to 25%%", consec_losses)

    held = [p.symbol for p in positions]
    if candidate in held:
        return {"notional": 0.0, "pct": 0.0, "reasons": [f"already hold {candidate}"]}
    if len(held) >= RISK["max_positions"]:
        return {"notional": 0.0, "pct": 0.0,
                "reasons": [f"max positions ({RISK['max_positions']}) reached"]}

    if held:
        avg_corr = avg_correlation_with_book(candidate, held)
        if avg_corr > RISK["correlation_penalty_threshold"]:
            pct *= 0.5
            reasons.append(f"avg corr with book {avg_corr:.2f} > "
                           f"{RISK['correlation_penalty_threshold']:.2f} → halved")

    # Sector concentration
    sector = sector_map.get(candidate, "Other")
    sector_value = sum(float(p.market_value) for p in positions
                       if sector_map.get(p.symbol, "Other") == sector)
    room = RISK["sector_cap_pct"] * equity - sector_value
    if room <= 0:
        return {"notional": 0.0, "pct": 0.0,
                "reasons": [f"sector {sector} at {RISK['sector_cap_pct']:.0%} cap"]}
    pct = min(pct, room / equity, RISK["max_position_pct"])

    notional = round(pct * equity, 2)

    # Cash reserve + availability
    max_spend = cash - RISK["cash_reserve_pct"] * equity
    if notional > max_spend:
        notional = round(max(max_spend, 0), 2)
        reasons.append(f"cash-reserve limited → ${notional:.2f}")
    if notional < RISK["min_notional_usd"]:
        return {"notional": 0.0, "pct": 0.0,
                "reasons": reasons + [f"below ${RISK['min_notional_usd']:.2f} minimum"]}

    return {"notional": notional, "pct": round(notional / equity, 4), "reasons": reasons}


# --------------------------------------------------------------------------
# The stop rule, enforced in code
# --------------------------------------------------------------------------

def rule_stop(entry: float, atr: float = 0.0) -> float:
    """The stop the thesis prompt asks for: 2x ATR below entry, never closer
    than stop_loss_pct (8%). min() of two prices = the WIDER stop."""
    fixed = entry * (1 - RISK["stop_loss_pct"])
    if atr and atr > 0:
        return round(min(entry - RISK["atr_multiplier"] * atr, fixed), 2)
    return round(fixed, 2)


def enforce_stop_rule(entry: float, stop: float, atr: float = 0.0):
    """Return (stop_to_use, note). A proposed stop tighter than the rule, or not
    below the entry at all, is widened to the rule; a wider one is kept (the
    2%-risk sizing cap already shrinks the position to pay for it).

    Why this exists: the prompt told the LLM "minimum 8% below entry" and the
    code accepted whatever came back. Every LLM stop in July was tighter than
    8% -- one sat 0.4% under its fill and stopped out the next day on noise.
    """
    if entry <= 0:
        return stop, None
    floor = rule_stop(entry, atr)
    if not (0 < stop < entry):
        return floor, f"stop {stop} not below entry {entry:.2f}; using rule stop {floor}"
    if stop > floor:
        return floor, (f"stop {stop} ({(entry - stop) / entry:.1%} below entry) tighter than "
                       f"rule; widened to {floor} ({(entry - floor) / entry:.1%})")
    return stop, None


# --------------------------------------------------------------------------
# Synthetic stop / target state
# --------------------------------------------------------------------------

class StateCorrupt(RuntimeError):
    """positions_state.json exists but cannot be read. Never treat it as
    empty: that adopts every position with a fresh default stop and then
    overwrites the evidence."""


def load_positions_state() -> dict:
    if not POSITIONS_STATE.exists():
        return {}
    try:
        state = json.loads(POSITIONS_STATE.read_text())
    except (OSError, ValueError) as e:
        raise StateCorrupt(f"{POSITIONS_STATE} unreadable: {e}") from e
    if not isinstance(state, dict):
        raise StateCorrupt(f"{POSITIONS_STATE} is not a JSON object")
    bad = [k for k, v in state.items() if not isinstance(v, dict) or "stop" not in v or "entry" not in v]
    if bad:
        raise StateCorrupt(f"{POSITIONS_STATE}: malformed entries {bad}")
    return state


def save_positions_state(state: dict):
    save_json(POSITIONS_STATE, state)


def record_entry(ticker: str, entry_price: float, notional: float,
                 stop_price: float, target_price: float, reason: str, **extra):
    """`extra` carries provenance: thesis_entry, entry_source ('fill'|'thesis'),
    order_id. entry_source 'thesis' means the fill wasn't confirmed at order
    time; manage re-anchors it to the broker's average on its next run."""
    state = load_positions_state()
    if ticker in state:
        # execute skips tracked tickers; this is the second guard. Overwriting
        # an entry wholesale once dropped a pending exit, so its filled
        # stop-out was never logged.
        raise ValueError(f"refusing to overwrite the tracked state for {ticker}")
    state[ticker] = {
        "entry": entry_price,
        "notional": notional,
        "stop": stop_price,
        "target": target_price,
        "opened": datetime.now().isoformat(),
        "trail_armed": False,
        "reason": reason,
        **extra,
    }
    save_json(POSITIONS_STATE, state)


def reconcile_entry(sym: str, st: dict, broker_avg: float) -> bool:
    """Re-anchor a tracked position's entry to the broker's average fill.

    The entry used to be the THESIS price forever -- 2% under the actual fill
    in one case -- so breakeven and the +10% trigger were computed from a price
    nobody paid. Stops stay absolute price levels, but are widened if the real
    entry leaves them tighter than the rule. Returns True if anything changed.
    """
    if broker_avg <= 0 or abs(st["entry"] - broker_avg) / broker_avg <= 0.001:
        return False
    st.setdefault("thesis_entry", st["entry"])
    old, old_stop = st["entry"], st["stop"]
    st["entry"] = round(broker_avg, 4)
    st["entry_source"] = "fill"
    if st.get("trail_armed"):
        # An armed breakeven only ever moves up: re-arm it on the real fill.
        st["stop"] = max(st["stop"], round(st["entry"] * 1.005, 2))
    else:
        st["stop"], _ = enforce_stop_rule(st["entry"], st["stop"])
    if st["stop"] < old_stop:
        st["_stop_lowered"] = {"from": old_stop, "to": st["stop"], "entry_from": old}
    if st.get("target") and st["target"] <= st["entry"]:
        st["target"] = round(st["entry"] * (1 + RISK["target_pct"]), 2)
    log.info("%s entry re-anchored to broker fill: %.2f -> %.2f (stop %.2f, target %s)",
             sym, old, st["entry"], st["stop"], st.get("target"))
    return True


def evaluate_exits(positions, state: dict) -> list[dict]:
    """Compare live positions to synthetic stops/targets; return sell actions.

    Mutates `state` in memory (adoption, entry re-anchoring, breakeven) and
    saves NOTHING: the caller decides when a change is safe to persist. It
    never deletes an entry -- a tracked position missing from the broker is
    the caller's to confirm (manage checks the broker per symbol before
    believing it is gone; one empty positions response once looked exactly
    like "everything was sold"). An entry with a pending exit is skipped (a
    sell is working). An entry with a pending buy whose shares the broker
    already holds is evaluated like any position. Returns "sell" actions and,
    for a position that could not be evaluated, an "unevaluated" action.
    """
    actions = []
    for p in positions:
        try:
            _evaluate_one(p, state, actions)
        except Exception as e:
            # One bad position (alpaca-py types current_price Optional) must not
            # abort every other stop. The caller alerts on "unevaluated".
            log.error("Could not evaluate %s: %s", getattr(p, "symbol", "?"), e)
            actions.append({"ticker": getattr(p, "symbol", "?"), "action": "unevaluated", "why": str(e)[:200]})
    return actions


def _evaluate_one(p, state: dict, actions: list):
    sym = p.symbol
    st = state.get(sym)
    if p.current_price is None:
        raise ValueError("broker returned no current price")
    price = float(p.current_price)
    if st is not None and st.get("pending_exit"):
        return                     # a sell is already working; manage resolves it
    if st is None:
        # Untracked position (opened outside this pipeline):
        # adopt it with a default stop below current price.
        st = {"entry": float(p.avg_entry_price),
              "notional": float(p.market_value or 0),
              "stop": round(price * (1 - RISK["stop_loss_pct"]), 2),
              "target": round(float(p.avg_entry_price) * (1 + RISK["target_pct"]), 2),
              "opened": datetime.now().isoformat(),
              "trail_armed": False, "reason": "adopted pre-existing position"}
        state[sym] = st
        log.warning("Adopted untracked position %s: stop %.2f target %.2f",
                    sym, st["stop"], st["target"])
    else:
        # Held shares are priced at what was actually paid -- including the
        # shares of a buy that is still working (a pending buy).
        reconcile_entry(sym, st, float(p.avg_entry_price))
    # A pending buy whose shares the broker already holds IS a position: its
    # stop applies to the held shares (manage cancels the working buy first).

    entry = st["entry"]
    # Breakeven: at +trail_trigger unrealized, raise the stop once
    if not st["trail_armed"] and entry > 0 and price >= entry * (1 + RISK["trail_trigger_pct"]):
        st["stop"] = max(st["stop"], round(entry * 1.005, 2))  # breakeven + slippage
        st["trail_armed"] = True
        log.info("%s +%.0f%% — stop raised to breakeven %.2f",
                 sym, RISK["trail_trigger_pct"] * 100, st["stop"])

    if price <= st["stop"]:
        actions.append({"ticker": sym, "action": "sell", "qty": p.qty,
                        "why": f"STOP hit: {price:.2f} <= {st['stop']:.2f} "
                               f"(entry {entry:.2f})"})
    elif st.get("target") and price >= st["target"]:
        actions.append({"ticker": sym, "action": "sell", "qty": p.qty,
                        "why": f"TARGET hit: {price:.2f} >= {st['target']:.2f} "
                               f"(entry {entry:.2f}, +{price / entry - 1:.1%})"})


def clear_position(ticker: str):
    state = load_positions_state()
    if ticker in state:
        del state[ticker]
        save_json(POSITIONS_STATE, state)
