"""Sovereign execution — turn signals into orders, enforce stops. PAPER FIRST.

Safety rails:
  - Refuses to run against a live account unless SOVEREIGN_LIVE_CONFIRM=yes
    is explicitly set (belt and suspenders on top of ALPACA_PAPER).
  - Circuit breaker checked before any entry.
  - Only trades when the market is open (fractional orders require DAY/market).
  - Every order is logged to sovereign_state/trade_log.jsonl with the full
    reasoning chain — signal components, risk sizing math, thesis.

How an order's life is tracked (rebuilt 2026-09-25 over three review rounds):
  - A state entry is removed only when the broker CONFIRMS: an exit's fill,
    or a buy's cancellation with nothing filled. Until then it carries
    `pending_exit` or `pending_buy`, and later runs check that order by id.
    Partial fills are real: a buy that filled partway is a position at its
    actual fill, and an exit that filled partway is logged for the shares it
    sold while the stop stays armed on the rest.
  - A market buy still unfilled after FILL_WAIT_S (a halt or pause) has its
    remainder canceled; whatever filled is the position.
  - execute sizes against the whole book: broker positions, tracked state
    (including pending buys), and open buy orders at the broker. It never
    buys a ticker that is already tracked, and reads state before any order.
  - The trade-log line is written BEFORE the state entry is deleted, and an
    order id already in the log is never logged twice.
  - Evidence is filtered: a recovered order must be alive (not canceled /
    expired / rejected), for this symbol, and inside the time window.
  - "Gone" is confirmed per symbol (get_open_position -> 404) before a
    tracked position is treated as closed; an empty positions list alone is
    not evidence, and a position the per-symbol check finds is evaluated.
  - Orders are safe to resend. alpaca-py resends ANY request that gets a
    429/504, POST and DELETE included. Buys carry a client_order_id; a
    recovered order must belong to THIS attempt; a buy whose outcome is
    unknown is recorded as pending and resolved by that id later.
  - Pending orders have a staleness bound (PENDING_STALE_H) and a replaced
    order is followed to its replacement.
  - Failures fail safe: an unreadable account skips only the circuit breaker
    and the snapshot; an unreadable clock falls back to exchange hours; a
    corrupt state file stops the run with an alert instead of being
    overwritten; state-save, snapshot and per-position failures are logged
    and the exits still go out; each exit is isolated from the others.
  - One run at a time. The lock records who holds it, and a manage run that
    finds it held too long alerts. Every broker call has an HTTP timeout.

Commands:
    python3 sovereign_execute.py execute   # act on the latest scan's signals
    python3 sovereign_execute.py manage    # enforce synthetic stops/targets
    python3 sovereign_execute.py status    # positions vs tracked state
"""

import argparse
import contextlib
import fcntl
import json
import math
import logging
import os
import time
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sovereign_config import (RESULTS_DIR, RISK, STATE_DIR, alpaca_keys, load_json, save_json,
                              with_timeout)
from risk_engine import (StateCorrupt, check_circuit_breaker, enforce_stop_rule, evaluate_exits,
                         load_positions_state, record_entry, save_positions_state, size_position)
from sovereign_alerts import alert

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("execute")

TRADE_LOG = STATE_DIR / "trade_log.jsonl"
ACCOUNT_SNAPSHOT = STATE_DIR / "account_snapshot.json"
LOCK_FILE = STATE_DIR / ".sovereign.lock"
MAX_SCAN_AGE_H = 2.0      # scans run hourly at :30, execute at :50; 2h tolerates one miss
FILL_WAIT_S = 20          # read at call time, so tests can set it
CANCEL_WAIT_S = 5
HTTP_TIMEOUT_S = 30
LOCK_ALERT_MIN = 10       # manage alerts if another run has held the lock this long
PENDING_STALE_H = 18      # a DAY order still pending after this is abnormal
# Terminal statuses. 'stopped', 'suspended', 'done_for_day', 'held' and the
# pending_* statuses are NOT terminal ('stopped' even guarantees a fill).
TERMINAL = {"canceled", "cancelled", "expired", "rejected"}
ET = ZoneInfo("America/New_York")

Outcome = namedtuple("Outcome", "state price qty replaced_by")
# state: 'filled' | 'partial' (terminal, some filled) | 'dead' (terminal, none
# filled) | 'open' (working; qty may be > 0) | 'replaced' | 'missing' (404) |
# 'unknown'


class UnknownOutcome(RuntimeError):
    """The broker may or may not have accepted an order, and we cannot tell."""


def _status(o) -> str:
    return str(getattr(o.status, "value", o.status)).lower()


def _qty(o) -> float:
    try:
        return float(o.filled_qty or 0)
    except (TypeError, ValueError):
        return 0.0


def _trading_client():
    from alpaca.trading.client import TradingClient
    key, secret, paper = alpaca_keys()
    if not paper and os.environ.get("SOVEREIGN_LIVE_CONFIRM", "") != "yes":
        raise RuntimeError(
            "ALPACA_PAPER=false but SOVEREIGN_LIVE_CONFIRM is not 'yes'. "
            "Refusing to trade live money. The strategy proves itself on paper first.")
    return with_timeout(TradingClient(key, secret, paper=paper), HTTP_TIMEOUT_S), paper


# --------------------------------------------------------------------------
# One run at a time
# --------------------------------------------------------------------------

@contextlib.contextmanager
def _run_lock(name: str):
    """Yields True with the lock held, or False if another run holds it. The
    holder writes its pid, command and start time into the lock file so a
    blocked run can say who is blocking it and for how long."""
    STATE_DIR.mkdir(exist_ok=True)
    with open(LOCK_FILE, "a+") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            try:   # the holder note is a courtesy; on a full disk the lock still holds
                # Written to the descriptor, not through fh. A buffered write that
                # fails at flush stays in fh's buffer, and close() retries it and
                # raises -- after the stops have gone out. os.write leaves nothing
                # behind to retry. (fh is O_APPEND, so after the truncate the note
                # lands at offset 0.)
                note = json.dumps({"pid": os.getpid(), "cmd": name, "since": datetime.now().isoformat()}).encode()
                os.ftruncate(fh.fileno(), 0)
                if os.write(fh.fileno(), note) != len(note):
                    log.warning("Lock-holder note written short; a blocked run will report the holder as unknown")
            except OSError as e:
                log.warning("Could not write the lock-holder note (%s); continuing", e)
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _lock_holder():
    try:
        return json.loads(LOCK_FILE.read_text() or "{}")
    except (OSError, ValueError):
        return {}


def _blocked(name: str):
    holder = _lock_holder()
    try:
        age_min = (datetime.now() - datetime.fromisoformat(holder["since"])).total_seconds() / 60
    except (KeyError, ValueError, TypeError):
        age_min = None
    log.warning("%s skipped: another run holds the lock (%s)", name, holder or "holder unknown")
    if name == "manage" and (age_min is None or age_min >= LOCK_ALERT_MIN):
        alert("Sovereign: stops NOT enforced — lock held",
              [f"{holder.get('cmd', '?')} pid {holder.get('pid', '?')} has held the lock "
               f"{'?' if age_min is None else f'{age_min:.0f} min'}; manage skipped."],
              level="critical", dedupe=False)


# --------------------------------------------------------------------------
# Broker helpers
# --------------------------------------------------------------------------

def _read(fn, *args, attempts: int = 3, wait: float = 2.0, **kwargs):
    """Retry a READ. Never wrap an order call in this."""
    for i in range(attempts):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if i == attempts - 1:
                raise
            log.warning("Broker read failed (%s); retry %d/%d", e, i + 1, attempts - 1)
            time.sleep(wait * (i + 1))


def _is_not_found(e: Exception) -> bool:
    code = getattr(e, "status_code", None)
    msg = str(e).lower()
    return code == 404 or "not found" in msg or "does not exist" in msg


def _is_transient(e: Exception) -> bool:
    """A failure that says nothing about whether the broker acted: any
    transport error (timeouts, dropped or reset connections, broken chunked
    responses, SSL, proxies, an unparseable body), 429 and 5xx. Only a clean
    4xx means the broker looked at the request and refused it."""
    code = getattr(e, "status_code", None)
    if code is not None:
        return code == 429 or code >= 500
    import requests
    if isinstance(e, requests.exceptions.HTTPError):
        resp = getattr(e, "response", None)
        return resp is None or resp.status_code == 429 or resp.status_code >= 500
    if isinstance(e, (requests.exceptions.RequestException, OSError, ValueError)):
        return True               # transport failure, or a body we could not parse
    try:
        import urllib3
        if isinstance(e, urllib3.exceptions.HTTPError):
            return True
    except ImportError:
        pass
    return False


def _order_outcome(client, order_id, timeout: float = None) -> Outcome:
    """Poll an order until it is filled, terminal, replaced, or the timeout."""
    timeout = FILL_WAIT_S if timeout is None else timeout
    deadline = time.monotonic() + timeout
    while True:
        try:
            o = client.get_order_by_id(order_id)
            st, q = _status(o), _qty(o)
            px = float(o.filled_avg_price) if o.filled_avg_price else None
            if st == "filled" and px:
                return Outcome("filled", px, q, None)
            if st == "replaced":
                return Outcome("replaced", px, q, str(getattr(o, "replaced_by", "") or "") or None)
            if st in TERMINAL:
                return Outcome("partial", px, q, None) if q > 0 and px else Outcome("dead", None, 0.0, None)
            out = Outcome("open", px, q, None)
        except Exception as e:
            if _is_not_found(e):
                return Outcome("missing", None, 0.0, None)
            log.warning("Order check failed for %s: %s", order_id, e)
            out = Outcome("unknown", None, 0.0, None)
        if time.monotonic() >= deadline:
            return out
        time.sleep(1.0)


def _recent_sell(client, ticker: str, since: datetime):
    """The newest LIVE sell order for `ticker` submitted after `since`: filled
    or still working, never canceled/expired/rejected. status=ALL matters --
    the API defaults to open orders only, which hides a sell that filled."""
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest
    orders = _read(client.get_orders, GetOrdersRequest(
        status=QueryOrderStatus.ALL, symbols=[ticker], after=since, limit=50))
    sells = [o for o in orders
             if str(getattr(o.side, "value", o.side)) == "sell" and o.symbol == ticker
             and (_status(o) not in TERMINAL or _qty(o) > 0) and _status(o) != "replaced"
             and o.submitted_at >= since]
    return max(sells, key=lambda o: o.submitted_at) if sells else None


def _usable_value(v) -> bool:
    """A market value sizing can use: a finite number. None, "" and NaN are not.
    A NaN passes float() and then switches the sector cap off silently, because
    min() ignores it and `room <= 0` is False (last order-safety check)."""
    try:
        return math.isfinite(float(v))
    except (TypeError, ValueError):
        return False


def _committed(o):
    """Dollars an open buy commits, or None when that cannot be known.

    Sovereign's own buys are notional. A qty-only market buy has no price until
    it fills, so it came from somewhere else, and guessing its cost would mean
    guessing how much cash is really spendable. A limit or stop-limit buy
    commits at most its limit. A stop buy without a limit becomes a market order
    once triggered and can fill anywhere above its trigger, so its cost is
    unknown too (order-safety review, 2026-09-26: pricing it at the trigger let
    a gap overspend the cash reserve). An option's limit is per share of a
    contract that usually, but not always, delivers 100 shares, so an option
    buy is unknown cost as well (narrow re-review: priced as qty x limit, it
    read at 1/100 of what it commits). Only us_equity and crypto are priced at
    all, and a cost that is not positive is unknown (final re-review: a
    multi-leg option parent carries no asset class and can carry a net credit)."""
    ac = getattr(o, "asset_class", None)
    if getattr(ac, "value", ac) not in ("us_equity", "crypto"):
        return None     # options, multi-leg parents (no asset class), anything unrecognised
    notional = float(o.notional or 0)
    if notional:
        return notional if notional > 0 else None
    qty = float(o.qty or 0)
    px = float(o.limit_price or 0)
    cost = qty * px if qty and px else None
    return cost if cost and cost > 0 else None


def _open_buys(client):
    from alpaca.trading.enums import OrderSide, QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest
    return list(_read(client.get_orders, GetOrdersRequest(
        status=QueryOrderStatus.OPEN, side=OrderSide.BUY, limit=500)))


def _close(client, ticker: str):
    """Submit a close; return the sell order (filled or working). If the
    request raises, the broker decides: this symbol's live sell from the last
    two minutes means the close went through (a resent 504 raises this way)."""
    started = datetime.now(timezone.utc)
    try:
        return client.close_position(ticker)
    except Exception as e:
        order = _recent_sell(client, ticker, started - timedelta(minutes=2))
        if order is not None:
            log.warning("Close %s raised (%s) but live sell order %s exists (%s) — using it",
                        ticker, e, order.id, _status(order))
            return order
        raise


def _confirm_gone(client, ticker: str):
    """(gone, position): gone only if the broker says the position does not
    exist; otherwise the Position it returned, to be evaluated."""
    try:
        return False, client.get_open_position(ticker)
    except Exception as e:
        if _is_not_found(e):
            return True, None
        raise


def _market_open(client):
    """(is_open, certain). An unreadable clock is NOT 'closed': it falls back
    to regular exchange hours so a clock outage cannot silently skip stops."""
    try:
        return bool(_read(client.get_clock).is_open), True
    except Exception as e:
        now = datetime.now(ET)
        guess = now.weekday() < 5 and (9, 30) <= (now.hour, now.minute) < (16, 0)
        log.error("Clock unreadable (%s); exchange-hours fallback says %s", e, "OPEN" if guess else "closed")
        return guess, False


def _age_h(iso: str) -> float:
    try:
        return (datetime.now() - datetime.fromisoformat(iso)).total_seconds() / 3600
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------------------
# The trade log
# --------------------------------------------------------------------------

def _logged_order_ids() -> set:
    try:
        ids = set()
        for line in TRADE_LOG.read_text().splitlines():
            if line.strip():
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("action") == "sell" and rec.get("order_id"):
                    ids.add((rec["order_id"], rec.get("partial", False)))
        return ids
    except FileNotFoundError:
        return set()


def _log_trade(record: dict):
    record["timestamp"] = datetime.now().isoformat()
    with open(TRADE_LOG, "ab") as f:
        # A torn append (disk full, a kill mid-write) leaves no newline, and
        # the next record would fuse onto it and become unreadable.
        if f.tell() > 0:
            with open(TRADE_LOG, "rb") as r:
                r.seek(-1, os.SEEK_END)
                if r.read(1) != b"\n":
                    f.write(b"\n")
        f.write((json.dumps(record, default=str) + "\n").encode())
        f.flush()
        os.fsync(f.fileno())


def _log_exit(ticker: str, why: str, order_id, fill_price, qty, st: dict, paper: bool,
              source: str, partial: bool = False) -> bool:
    """Write one exit record. False (and nothing written) if this order id is
    already logged the same way -- two runs, or a reconcile after a manage,
    must not count one exit twice."""
    if order_id and (str(order_id), partial) in _logged_order_ids():
        log.info("Exit for %s (order %s) already logged — skipping duplicate", ticker, order_id)
        return False
    entry = float((st or {}).get("entry") or 0)
    pnl_pct = round(fill_price / entry - 1, 5) if fill_price and entry else None
    _log_trade({"action": "sell", "ticker": ticker, "why": why, "source": source,
                "order_id": str(order_id) if order_id else None, "fill_price": fill_price,
                "qty": qty, "partial": partial, "entry": entry or None, "pnl_pct": pnl_pct,
                "paper": paper})
    return True


# --------------------------------------------------------------------------
# Which scan to act on
# --------------------------------------------------------------------------

def _latest_run():
    """(scan, signals, why_not). The scan and its signal snapshot must come
    from the SAME run and be fresh.

    This used to take the newest scan and the newest signals file separately.
    A scan that died after writing signals would pair fresh signals with the
    previous run's theses. Scans now name their snapshot (`signals_file`);
    older scans fall back to "the snapshot written just before this scan".
    """
    scans = sorted(RESULTS_DIR.glob("scan_*.json"), reverse=True)
    if not scans:
        return None, None, "no scan yet"
    scan = load_json(scans[0], {})
    stamp = scans[0].stem.split("_", 1)[1]
    try:
        age_h = (datetime.now() - datetime.strptime(stamp, "%Y%m%d_%H%M%S")).total_seconds() / 3600
    except ValueError:
        return None, None, f"unparseable scan name {scans[0].name}"
    if age_h > MAX_SCAN_AGE_H:
        return None, None, f"newest scan is {age_h:.1f}h old (limit {MAX_SCAN_AGE_H}h)"
    if not scan.get("theses"):
        return scan, None, scan.get("halted") or "newest scan has no theses"
    if scan.get("signals_file"):
        sigs = load_json(RESULTS_DIR / scan["signals_file"], None)
        if not sigs:
            return None, None, f"scan names {scan['signals_file']} but it is missing"
        return scan, sigs, None
    snaps = sorted(p for p in RESULTS_DIR.glob("signals_*.json") if p.name <= f"signals_{stamp}.json")
    if not snaps:
        return None, None, "no signal snapshot precedes the newest scan"
    snap_stamp = snaps[-1].stem.split("_", 1)[1]
    gap_min = (datetime.strptime(stamp, "%Y%m%d_%H%M%S")
               - datetime.strptime(snap_stamp, "%Y%m%d_%H%M%S")).total_seconds() / 60
    if gap_min > 30:
        return None, None, f"snapshot {snaps[-1].name} is {gap_min:.0f} min older than the scan"
    return scan, load_json(snaps[-1], {}), None


# --------------------------------------------------------------------------
# Buying
# --------------------------------------------------------------------------

def _client_order_id(ticker: str, run_key: str) -> str:
    return f"sov-{run_key}-{ticker}"[:48]


def _submit_notional_buy(client, ticker: str, notional: float, client_order_id: str,
                         attempt_start: datetime):
    """Submit a buy. Returns the order, None if this client_order_id belongs to
    an EARLIER attempt (already traded on this scan: skip), or raises.
    Raises UnknownOutcome when the broker may have the order but cannot say."""
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest
    try:
        return client.submit_order(MarketOrderRequest(
            symbol=ticker, notional=round(notional, 2), client_order_id=client_order_id,
            side=OrderSide.BUY, time_in_force=TimeInForce.DAY))
    except Exception as submit_err:
        try:
            order = _read(client.get_order_by_client_id, client_order_id)
        except Exception as lookup_err:
            if _is_not_found(lookup_err) and not _is_transient(submit_err):
                raise submit_err            # a clean rejection the broker never recorded
            # A timeout or 5xx on submit followed by a 404 can just mean the
            # order is still being processed: that is not evidence of failure.
            raise UnknownOutcome(f"buy {ticker}: submit raised ({submit_err}); lookup "
                                 f"{'404' if _is_not_found(lookup_err) else 'failed'} ({lookup_err})"
                                 ) from lookup_err
        submitted = getattr(order, "submitted_at", None)
        if ((_status(order) in TERMINAL and _qty(order) == 0)
                or (submitted and submitted < attempt_start - timedelta(seconds=60))):
            log.warning("Buy %s: client_order_id %s belongs to an earlier attempt (%s, %s) — "
                        "not recording it as a new position", ticker, client_order_id,
                        _status(order), submitted)
            return None
        log.warning("Buy %s raised (%s) but the broker has this attempt's order %s (%s) — "
                    "treating as placed", ticker, submit_err, order.id, _status(order))
        return order


def execute():
    with _run_lock("execute") as got:
        if not got:
            _blocked("execute")
            return
        _execute()


def _execute():
    client, paper = _trading_client()
    account = _read(client.get_account)

    if check_circuit_breaker(account):
        log.error("HALTED — circuit breaker active. No new entries today.")
        alert("Circuit breaker active", ["No new entries today."], level="critical")
        return

    is_open, certain = _market_open(client)
    if not (is_open and certain):
        log.info("Market closed or clock unknown — entries need a confirmed open market. Skipping.")
        return

    # State before any order: a corrupt file must stop the run, not surface
    # after a buy has already gone out with nowhere to record it.
    try:
        state = load_positions_state()
    except StateCorrupt as e:
        log.error("%s — no entries this run", e)
        alert("Sovereign state file CORRUPT — no entries", [str(e)], level="critical")
        return

    scan, sigs, why_not = _latest_run()
    if why_not:
        log.info("Not executing: %s.", why_not)
        return

    positions = list(_read(client.get_all_positions))
    if not positions:
        positions = list(_read(client.get_all_positions))   # one empty response is not evidence
    open_buys = _open_buys(client)
    unvalued = [p.symbol for p in positions if not _usable_value(getattr(p, "market_value", None))]
    if unvalued:
        log.error("Position(s) with no market value: %s — no entries this run", unvalued)
        alert("Sovereign: position of unknown value — no entries",
              [f"{', '.join(unvalued)}: the broker returned no usable market value, so the sector "
               "cap cannot be checked."], level="critical")
        return
    unknown = [o for o in open_buys if _committed(o) is None]
    if unknown:
        names = ", ".join(f"{o.symbol} ({o.qty} sh)" for o in unknown)
        log.error("Open buy of unknown cost: %s — no entries this run", names)
        alert("Sovereign: open buy of unknown cost — no entries",
              [f"{names}: its cost cannot be known before it fills (no notional or limit price, "
               "or an option), so the cash it commits is unknown.",
               "Sovereign submits only notional buys; this order came from somewhere else."],
              level="critical")
        return
    held = {p.symbol for p in positions}
    equity = float(account.equity)
    cash = float(account.cash)
    # The book = broker positions + every tracked entry + open buy orders.
    # Positions alone let a still-working buy be bought a second time, and an
    # empty positions response would let one run blow through max_positions.
    # Money committed to buys that have not filled is not spendable cash.
    # Every open buy's UNFILLED part is committed, whether or not some of it has
    # already filled (a held, partly filled, still-working buy included).
    open_ids, extra = set(), {}
    for o in open_buys:
        open_ids.add(str(o.id))
        full = _committed(o)
        filled = _qty(o) * float(o.filled_avg_price or 0)
        remainder = max(full - filled, 0.0)
        cash -= remainder
        if o.symbol not in held:
            positions.append(SimpleNamespace(symbol=o.symbol, market_value=full))
            held.add(o.symbol)
        else:
            # Already on the book (held, or an earlier open buy): its sector
            # carries the unfilled remainder too. Folded into the one entry the
            # symbol has, since a second entry would count toward max_positions.
            extra[o.symbol] = extra.get(o.symbol, 0.0) + remainder
    if extra:
        positions = [SimpleNamespace(symbol=p.symbol, market_value=float(p.market_value) + extra[p.symbol])
                     if p.symbol in extra else p for p in positions]
    for sym, st in state.items():
        pb = st.get("pending_buy") or {}
        if sym not in held:
            notional = float(st.get("notional") or 0)
            positions.append(SimpleNamespace(symbol=sym, market_value=notional))
            held.add(sym)
            if pb and str(pb.get("order_id")) not in open_ids:
                cash -= notional      # an unknown-outcome buy not visible as an open order
    tracked = set(state) | {o.symbol for o in open_buys}
    regime_mult = (scan.get("macro") or {}).get("exposure_multiplier", 0.75)
    run_key = scan.get("run_id") or datetime.now().strftime("%Y%m%d_%H%M")

    theses = {t["ticker"]: t for t in scan.get("theses", [])}
    from sovereign_pipeline import SECTOR_MAP

    candidates = []
    for ticker, s in sigs.get("signals", {}).items():
        if s["conviction"] not in ("high", "medium"):
            continue
        th = theses.get(ticker, {})
        if th.get("direction") != "buy":
            continue  # Claude veto: composite hot but thesis says no → no trade
        candidates.append((s["score"], ticker, s, th))
    candidates.sort(reverse=True)

    if not candidates:
        log.info("No candidates pass composite+thesis agreement. Patience is a position.")
        return

    placed = []
    for score, ticker, s, th in candidates:
        if ticker in tracked:
            log.info("SKIP %s: already tracked (state or an open order)", ticker)
            continue
        earnings_flag = any("Earnings" in f for f in s.get("risk_flags", []))
        entry = float(th.get("entry_price") or 0)
        stop = float(th.get("stop_loss") or 0)
        stop_pct = (entry - stop) / entry if entry > 0 and 0 < stop < entry else RISK["stop_loss_pct"]
        # The pipeline enforces the ATR/8% rule on every thesis; this is the
        # last line for scans written before it did. Never closer than 8%.
        stop_pct = max(stop_pct, RISK["stop_loss_pct"])

        # `positions` includes buys made earlier in THIS run (appended below).
        sizing = size_position(equity, cash, s["conviction"], regime_mult,
                               positions, SECTOR_MAP, ticker,
                               stop_pct=stop_pct, earnings_imminent=earnings_flag)
        if sizing["notional"] <= 0:
            log.info("SKIP %s: %s", ticker, "; ".join(sizing["reasons"]))
            continue

        coid = _client_order_id(ticker, run_key)
        attempt_start = datetime.now(timezone.utc)
        thesis_entry = entry
        target = float(th.get("target") or 0)
        try:
            order = _submit_notional_buy(client, ticker, sizing["notional"], coid, attempt_start)
        except UnknownOutcome as e:
            # The order may exist. Record it as pending by client_order_id so
            # later runs count it and manage resolves it; buy nothing else now.
            log.error("%s — recorded as pending, no further buys this run", e)
            _record_buy(ticker, thesis_entry, sizing["notional"], thesis_entry * (1 - stop_pct),
                        target or thesis_entry * (1 + RISK["target_pct"]), th, paper,
                        order_id=None, coid=coid, status="unknown", fill_px=None, qty=0.0,
                        pending=True, score=score, s=s, sizing=sizing, run_id=scan.get("run_id"))
            alert(f"Buy outcome UNKNOWN: {ticker}", [str(e)[:300], "Recorded as pending; no further buys."],
                  level="critical", dedupe=False)
            break
        except Exception as e:
            log.error("Order failed for %s: %s", ticker, e)
            continue
        if order is None:
            continue

        out = _order_outcome(client, order.id)
        if out.state in ("open", "unknown", "replaced"):
            # A market order unfilled after FILL_WAIT_S means a halt or pause.
            # Cancel the rest; whatever filled is the position.
            try:
                client.cancel_order_by_id(order.id)
            except Exception as e:
                log.warning("Cancel of unfilled buy %s (%s) failed: %s", ticker, order.id, e)
            out = _order_outcome(client, order.id, timeout=CANCEL_WAIT_S)
        if out.state == "dead":
            log.warning("Buy %s order %s was canceled/rejected with nothing filled — nothing recorded",
                        ticker, order.id)
            continue
        filled = out.state in ("filled", "partial")
        # A still-working buy that has filled partway is priced at its fill.
        entry = out.price if (filled or (out.qty > 0 and out.price)) else thesis_entry
        notional = round(out.price * out.qty, 2) if filled else sizing["notional"]
        stop_price = round(entry * (1 - stop_pct), 2) if entry > 0 else 0
        tgt = target if target and target > entry else round(entry * (1 + RISK["target_pct"]), 2)
        _record_buy(ticker, entry, notional, stop_price, tgt, th, paper, order_id=str(order.id),
                    coid=coid, status=out.state, fill_px=out.price, qty=out.qty, pending=not filled,
                    score=score, s=s, sizing=sizing, run_id=scan.get("run_id"), thesis_entry=thesis_entry)
        placed.append(f"{ticker} ${notional:.2f} (stop {stop_price}, target {tgt})"
                      + ("" if out.state == "filled" else f" [{out.state}]"))
        log.info("BUY %s $%.2f @ %s — %s", ticker, notional,
                 f"{entry:.2f} {out.state}" if filled else f"{entry:.2f} thesis (fill pending)",
                 "; ".join(sizing["reasons"]))
        positions.append(SimpleNamespace(symbol=ticker, market_value=notional))
        tracked.add(ticker)
        cash -= notional

    if placed:
        alert("Orders placed" + (" [PAPER]" if paper else " [LIVE]"),
              placed, level="signal", dedupe=False)


def _record_buy(ticker, entry, notional, stop_price, target, th, paper, *, order_id, coid,
                status, fill_px, qty, pending, score, s, sizing, run_id, thesis_entry=None):
    """Trade-log line first, then state. If state cannot be written the buy
    is still on the record, and the alert names the order."""
    thesis_entry = entry if thesis_entry is None else thesis_entry
    try:
        _log_trade({"action": "buy", "ticker": ticker, "notional": notional,
                    "composite": score, "conviction": s["conviction"],
                    "sizing": sizing["reasons"], "stop": round(stop_price, 2), "target": round(target, 2),
                    "entry": entry, "thesis_entry": thesis_entry, "fill_price": fill_px, "qty": qty,
                    "fill_status": status, "thesis": th.get("reasoning", ""), "order_id": order_id,
                    "client_order_id": coid, "run_id": run_id, "paper": paper})
    except Exception as e:
        log.error("Buy %s (order %s) not written to the trade log: %s", ticker, order_id or coid, e)
        alert(f"Buy NOT in trade log: {ticker}", [f"order {order_id or coid}", str(e)[:300]],
              level="critical", dedupe=False)
    extra = {"thesis_entry": thesis_entry, "entry_source": "thesis" if pending else "fill",
             "order_id": order_id, "client_order_id": coid}
    if pending:
        extra["pending_buy"] = {"order_id": order_id, "client_order_id": coid,
                                "since": datetime.now().isoformat()}
    try:
        record_entry(ticker, entry, notional, round(stop_price, 2), round(target, 2),
                     reason=th.get("reasoning", ""), **extra)
    except Exception as e:
        log.error("Bought %s (order %s) but could not record state: %s", ticker, order_id or coid, e)
        alert(f"Buy NOT recorded in state: {ticker}",
              [f"order {order_id or coid}", str(e)[:300], "manage will adopt it with a default stop."],
              level="critical", dedupe=False)
    try:
        from sovereign_memory import SovereignMemory
        from sovereign_pipeline import SECTOR_MAP
        mem = SovereignMemory()
        mem.write_entry(ticker, entry, s["conviction"], th.get("reasoning", ""),
                        str(s.get("explain", "")), SECTOR_MAP.get(ticker, ""), score)
        mem.close()
    except Exception as e:
        log.debug("Memory write failed: %s", e)


# --------------------------------------------------------------------------
# manage
# --------------------------------------------------------------------------

def write_account_snapshot(account, positions, paper: bool, state: dict = None):
    """The one documented account view for anything outside the pipeline
    (session hooks, the watcher). Written every manage run. Keys are a
    contract tested in tests/test_order_path.py."""
    state = load_positions_state() if state is None else state
    save_json(ACCOUNT_SNAPSHOT, {
        "timestamp": datetime.now().isoformat(),
        "paper": paper,
        "equity": float(account.equity),
        "cash": float(account.cash),
        "last_equity": float(account.last_equity or account.equity),
        "positions": [{
            "symbol": p.symbol,
            "qty": float(p.qty),
            "avg_entry": float(p.avg_entry_price),
            "current": float(p.current_price) if p.current_price is not None else None,
            "unrealized_pl": float(p.unrealized_pl) if p.unrealized_pl is not None else None,
            "unrealized_plpc": float(p.unrealized_plpc) if p.unrealized_plpc is not None else None,
            "stop": state.get(p.symbol, {}).get("stop"),
            "target": state.get(p.symbol, {}).get("target"),
        } for p in positions],
    })


def manage():
    with _run_lock("manage") as got:
        if not got:
            _blocked("manage")
            return
        _manage()


def _save(state):
    """Persist state; a failure is alerted but never stops the exits."""
    try:
        save_positions_state(state)
        return True
    except Exception as e:
        log.error("State save failed (exits continue): %s", e)
        alert("Sovereign: state save FAILED", [str(e)[:300], "Exits continue; state may be stale."],
              level="critical", dedupe=False)
        return False


def _resolve_filled_buy(st: dict, out: Outcome):
    """A pending buy that filled (fully or in part) becomes a position at its
    actual fill. An armed breakeven only moves up; otherwise the stop is
    re-checked against the rule from the real entry."""
    st["entry"], st["entry_source"] = round(out.price, 4), "fill"
    st["notional"] = round(out.price * out.qty, 2)
    if st.get("trail_armed"):
        st["stop"] = max(st["stop"], round(st["entry"] * 1.005, 2))
    else:
        st["stop"], _ = enforce_stop_rule(st["entry"], st["stop"])
    if st.get("target") and st["target"] <= st["entry"]:
        st["target"] = round(st["entry"] * (1 + RISK["target_pct"]), 2)
    st.pop("pending_buy", None)


def _void_buy(sym: str, pb: dict, why: str, paper: bool):
    """The buy line is already in the trade log; say that it never became a
    position, so the log reconciles."""
    log.warning("%s buy %s: %s — dropping state, no position", sym,
                pb.get("order_id") or pb.get("client_order_id"), why)
    try:
        _log_trade({"action": "buy_void", "ticker": sym, "order_id": pb.get("order_id"),
                    "client_order_id": pb.get("client_order_id"), "why": why, "paper": paper})
    except Exception as e:
        log.error("Could not record buy_void for %s: %s", sym, e)


def _finish_exit(state, sym, why, order_id, fill_px, qty, paper, source):
    """Log, then delete. The order matters: a failure between the two leaves
    the state (and a retry next run) rather than a deleted stop and no record."""
    st = state.get(sym, {})
    _log_exit(sym, why, order_id, fill_px, qty, st, paper, source)
    state.pop(sym, None)
    _save(state)


def _partial_exit(state, sym, why, order_id, fill_px, qty, paper, source):
    """Log the shares a dead exit order did sell; keep the stop on the rest."""
    st = state.get(sym, {})
    _log_exit(sym, why + " (partial fill)", order_id, fill_px, qty, st, paper, source, partial=True)
    st.pop("pending_exit", None)
    _save(state)
    alert(f"Partial exit: {sym}", [f"order {order_id} sold {qty} @ {fill_px}; stop re-armed on the rest"],
          level="warning", dedupe=False)


def _manage():
    client, paper = _trading_client()

    try:
        account = _read(client.get_account)
    except Exception as e:
        account = None
        log.error("Account unreadable (%s): enforcing stops without the circuit breaker", e)
        alert("Sovereign: account unreadable", [str(e)[:300], "Stops still enforced."],
              level="critical", dedupe=False)
    if account is not None:
        try:
            check_circuit_breaker(account)  # trips halt for today if breached
        except Exception as e:
            log.error("Circuit breaker check failed (exits continue): %s", e)
            alert("Sovereign: circuit breaker check FAILED", [str(e)[:300], "Exits continue."],
                  level="critical", dedupe=False)

    try:
        state = load_positions_state()
    except StateCorrupt as e:
        log.error("%s — refusing to run (stops NOT enforced this cycle)", e)
        alert("Sovereign state file CORRUPT — stops not enforced", [str(e)], level="critical", dedupe=False)
        return

    try:
        positions = list(_read(client.get_all_positions))
        if not positions:
            positions = list(_read(client.get_all_positions))   # one empty response is not evidence
    except Exception as e:
        log.error("Positions unreadable (%s) — stops NOT enforced this cycle", e)
        alert("Sovereign: positions unreadable — stops not enforced", [str(e)[:300]], level="critical",
              dedupe=False)
        return
    held = {p.symbol for p in positions}

    # 1. Buys whose outcome was not confirmed at order time
    for sym in [s for s, st in state.items() if st.get("pending_buy")]:
        st = state[sym]
        pb = st["pending_buy"]
        try:
            # Staleness first, so an order that can never be read still alerts.
            if _age_h(pb.get("since")) > PENDING_STALE_H:
                alert(f"Pending buy STALE: {sym}", [f"order {pb.get('order_id') or pb.get('client_order_id')} "
                                                    f"unresolved after {_age_h(pb.get('since')):.0f}h"],
                      level="critical")
            if not pb.get("order_id"):
                try:
                    o = _read(client.get_order_by_client_id, pb["client_order_id"])
                    pb["order_id"] = str(o.id)
                except Exception as e:
                    if _is_not_found(e):
                        _void_buy(sym, pb, "never placed", paper)
                        del state[sym]
                        _save(state)
                        continue
                    raise
            out = _order_outcome(client, pb["order_id"], timeout=0)
            if out.state == "replaced" and out.replaced_by:
                pb["order_id"] = out.replaced_by
            elif out.state in ("filled", "partial"):
                _resolve_filled_buy(st, out)
                log.info("%s pending buy %s @ %.2f x %.4f", sym, out.state, out.price, out.qty)
            elif out.state in ("dead", "missing"):
                _void_buy(sym, pb, f"order {out.state} with nothing filled", paper)
                del state[sym]
            _save(state)
        except Exception as e:
            log.error("Resolving pending buy for %s failed: %s", sym, e)

    # 2. Exits submitted earlier whose fill was not confirmed
    for sym in [s for s, st in state.items() if st.get("pending_exit")]:
        st = state[sym]
        pe = st["pending_exit"]
        try:
            out = _order_outcome(client, pe["order_id"], timeout=0)
            why = pe.get("why", "exit filled")
            if out.state == "filled":
                _finish_exit(state, sym, why, pe["order_id"], out.price, out.qty, paper, "manage")
                log.info("SELL %s filled @ %.2f (pending exit resolved)", sym, out.price)
            elif out.state == "partial":
                _partial_exit(state, sym, why, pe["order_id"], out.price, out.qty, paper, "manage")
            elif out.state == "replaced" and out.replaced_by:
                pe["order_id"] = out.replaced_by
                _save(state)
            elif out.state == "missing":
                del st["pending_exit"]
                _save(state)
                alert(f"Exit order not found: {sym}", [f"order {pe['order_id']} does not exist; stop re-armed"],
                      level="warning", dedupe=False)
            elif out.state == "dead":
                del st["pending_exit"]
                _save(state)
                alert(f"Exit order died: {sym}", [f"order {pe['order_id']} canceled/expired with nothing "
                                                   f"filled; original stop {st['stop']} re-armed"],
                      level="critical", dedupe=False)
            elif _age_h(pe.get("since")) > PENDING_STALE_H:
                alert(f"Pending exit STALE: {sym}", [f"order {pe['order_id']} still {out.state} after "
                                                     f"{_age_h(pe.get('since')):.0f}h"], level="critical")
                if out.state == "unknown" and sym in held:
                    del st["pending_exit"]          # unreadable for a day: re-evaluate the stop
                    _save(state)
        except Exception as e:
            log.error("Resolving pending exit for %s failed: %s", sym, e)

    # 3. Tracked positions the broker's list does not show: confirm per symbol
    for sym in [s for s in state if s not in held
                and not state[s].get("pending_buy") and not state[s].get("pending_exit")]:
        st = state[sym]
        try:
            gone, pos = _confirm_gone(client, sym)
            if not gone:
                positions.append(pos)             # the list was wrong; evaluate what the broker returned
                held.add(sym)
                continue
            opened = datetime.fromisoformat(st.get("opened") or "2000-01-01T00:00:00")
            order = _recent_sell(client, sym, opened.astimezone(timezone.utc))
            if order is not None:
                out = _order_outcome(client, order.id, timeout=0)
                if out.state in ("filled", "partial"):
                    _finish_exit(state, sym, "closed outside manage (reconciled from broker)",
                                 order.id, out.price, out.qty, paper, "reconciled")
                    alert(f"Unlogged exit reconciled: {sym}", [f"order {order.id} @ {out.price}"],
                          level="warning", dedupe=False)
                    continue
                st["pending_exit"] = {"order_id": str(order.id), "since": datetime.now().isoformat(),
                                      "why": "closed outside manage"}
                _save(state)
                continue
            # Confirmed gone, no sell anywhere: a record, but not a trade.
            _log_trade({"action": "reconcile_unresolved", "ticker": sym, "paper": paper,
                        "why": "broker has no position and no sell since it opened", "state": st})
            state.pop(sym, None)
            _save(state)
            alert(f"Position vanished with no sell: {sym}", ["Recorded as unresolved, not as a trade."],
                  level="critical", dedupe=False)
        except Exception as e:
            log.error("Could not reconcile %s (%s) — keeping its state for the next run", sym, e)

    # 4. Adopt, re-anchor, breakeven, decide exits
    actions = evaluate_exits(positions, state)
    _save(state)
    bad = [a for a in actions if a["action"] == "unevaluated"]
    if bad:
        alert("Sovereign: stops NOT evaluated", [f"{a['ticker']}: {a['why']}" for a in bad], level="critical",
              dedupe=False)
    for sym, st in state.items():
        lowered = st.pop("_stop_lowered", None)
        if lowered:
            alert(f"Stop lowered on re-anchor: {sym}",
                  [f"entry {lowered['entry_from']} -> {st['entry']} (broker fill); "
                   f"stop {lowered['from']} -> {lowered['to']}"], level="warning", dedupe=False)
    if any(True for _ in state):
        _save(state)

    if account is not None:
        try:
            write_account_snapshot(account, positions, paper, state)
        except Exception as e:  # reporting must never stop stop-enforcement
            log.error("Account snapshot failed (exits continue): %s", e)
            alert("Sovereign: account snapshot failed", [str(e)[:300], "Exits continue."],
                  level="warning", dedupe=False)

    sells = [a for a in actions if a["action"] == "sell"]
    if not sells:
        if not positions:
            log.info("No positions to manage.")
        return
    is_open, certain = _market_open(client)
    if not certain:
        alert("Sovereign: clock unreadable with exits pending",
              [f"{len(sells)} exit(s); exchange-hours fallback says {'OPEN' if is_open else 'closed'}"],
              level="critical", dedupe=False)
    if not is_open:
        log.info("Market closed — %d exit(s) queue for the next open session.", len(sells))
        return

    for action in sells:
        ticker = action["ticker"]
        try:
            pb = state.get(ticker, {}).get("pending_buy")
            if pb:
                # Alpaca rejects a sell while our own buy is still working
                # (wash-trade protection), and a cancel is asynchronous: the buy
                # must be TERMINAL before the close, and a late fill is ours.
                if pb.get("order_id"):
                    try:
                        client.cancel_order_by_id(pb["order_id"])
                    except Exception as e:
                        log.warning("Cancel of pending buy %s before the exit failed: %s", ticker, e)
                    bo = _order_outcome(client, pb["order_id"], timeout=CANCEL_WAIT_S)
                else:
                    bo = Outcome("unknown", None, 0.0, None)
                if bo.state in ("filled", "partial"):
                    _resolve_filled_buy(state[ticker], bo)
                    _save(state)
                elif bo.state in ("dead", "missing"):
                    del state[ticker]["pending_buy"]
                    _save(state)
                else:
                    alert(f"Exit waiting on a buy cancel: {ticker}",
                          [action["why"], f"buy {pb.get('order_id') or pb.get('client_order_id')} still "
                           f"{bo.state}; stop stays armed and is retried next run"],
                          level="critical", dedupe=False)
                    continue
            order = _close(client, ticker)
            out = _order_outcome(client, order.id)
            if out.state == "filled":
                _finish_exit(state, ticker, action["why"], order.id, out.price, out.qty, paper, "manage")
                log.info("SELL %s — %s @ %.2f", ticker, action["why"], out.price)
                alert(f"Exit: {ticker}", [f"{action['why']} @ {out.price:.2f}"], level="warning", dedupe=False)
                _memory_exit(ticker, action["why"], out.price)
            elif out.state == "partial":
                _partial_exit(state, ticker, action["why"], order.id, out.price, out.qty, paper, "manage")
            elif out.state == "dead":
                log.error("Exit order for %s was canceled/rejected; stop stays armed", ticker)
                alert(f"Exit REJECTED: {ticker}", [action["why"], f"order {order.id}"],
                      level="critical", dedupe=False)
            else:
                state[ticker]["pending_exit"] = {"order_id": str(order.id), "why": action["why"],
                                                 "since": datetime.now().isoformat()}
                _save(state)
                log.warning("Exit for %s submitted (order %s) but not filled yet — pending",
                            ticker, order.id)
                alert(f"Exit pending: {ticker}", [action["why"], f"order {order.id} not filled yet"],
                      level="warning", dedupe=False)
        except Exception as e:
            log.error("Exit failed for %s: %s", ticker, e)
            alert(f"Exit FAILED: {ticker}", [action["why"], str(e)[:300]],
                  level="critical", dedupe=False)


def _memory_exit(ticker, why, px):
    try:
        from sovereign_memory import SovereignMemory
        reason = "stop" if "STOP" in why.upper() else "target" if "TARGET" in why.upper() else "manual"
        mem = SovereignMemory()
        mem.write_exit(ticker, px or 0, reason, 0)
        mem.close()
    except Exception as e:
        log.debug("Memory exit write failed: %s", e)


def status():
    client, paper = _trading_client()
    account = client.get_account()
    positions = client.get_all_positions()
    state = load_positions_state()

    print(f"\nSOVEREIGN EXECUTION STATUS — {'PAPER' if paper else 'LIVE'}")
    print(f"Equity ${float(account.equity):.2f} | Cash ${float(account.cash):.2f} | "
          f"Halted: {load_json(STATE_DIR / 'halt.json', {}).get('date') == datetime.now().strftime('%Y-%m-%d')}")
    for p in positions:
        st = state.get(p.symbol, {})
        print(f"  {p.symbol:6s} {float(p.qty):.4f} @ {float(p.avg_entry_price):.2f} → "
              f"{p.current_price} | stop {st.get('stop', '—')} target {st.get('target', '—')}"
              f"{' [breakeven armed]' if st.get('trail_armed') else ''}"
              f"{' [exit pending]' if st.get('pending_exit') else ''}"
              f"{' [buy pending]' if st.get('pending_buy') else ''}")
    orphans = [s for s in state if s not in {p.symbol for p in positions}]
    if orphans:
        print(f"  Tracked but not held: {', '.join(orphans)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sovereign execution")
    parser.add_argument("command", choices=["execute", "manage", "status"])
    args = parser.parse_args()
    {"execute": execute, "manage": manage, "status": status}[args.command]()
