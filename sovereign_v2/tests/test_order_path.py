"""Order-path tests for the 2026-09-25 rebuild, against a fake broker that
behaves like real Alpaca.

Written by someone who is NOT the author of the code under test. Expected
behaviour comes from three places only: the rules in sovereign_execute.py's
module docstring, the round-1 adversarial findings (numbered F1..F15 below),
and alpaca-py's real semantics (read from its source):

  - Orders are pydantic `alpaca.trading.models.Order` objects: UUID ids,
    tz-aware submitted_at, numeric fields as STRINGS, filled_avg_price None
    until something fills. New orders are born pending_new, not filled.
  - GetOrdersRequest with no status means status=open (the server default):
    a filled or canceled order is invisible unless status=closed/all.
    symbols / side / after / until / limit (default 50, max 500) / direction
    (default desc) are all honoured.
  - close_position, get_open_position, get_order_by_id and
    get_order_by_client_id raise APIError with status_code 404 when the thing
    is absent. A duplicate client_order_id is a 422.
  - alpaca-py resends ANY request (POST and DELETE included) on 429/504, so
    "the request raised" never proves "the broker did nothing".

and Alpaca server behaviour that is NOT in the SDK, modelled in the fakes:

  - Wash-trade protection: a market order while an opposite-side order for
    the same symbol is still open (pending_cancel included) is rejected 403
    'potential wash trade detected'.
  - DELETE /orders/{id} is asynchronous: 204, then pending_cancel, then
    canceled on a later read (or a fill, if the venue got there first).
  - Fractional and notional orders must be DAY; a notional is limited to two
    decimal places (422 otherwise). The order echoes the request's TIF.
  - The Basic data plan refuses historical bars whose end is inside the last
    15 minutes (403 'subscription does not permit querying recent SIP data');
    with no end the server defaults to now-15min.

A test that fails here is a finding: it is left failing, never bent to fit
the implementation. No network: an autouse guard blocks sockets and HTTP.

Run:  python3 -m pytest tests/test_order_path.py -q -p no:cacheprovider
"""
import ast
import errno
import fcntl
import json
import os
import pathlib
import socket
import sys
import types
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import requests

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
# Force fake credentials: a shell with real keys exported must not leak them in.
os.environ["ALPACA_API_KEY"] = "TEST"
os.environ["ALPACA_SECRET_KEY"] = "TEST"
os.environ["ALPACA_PAPER"] = "true"
os.environ.pop("DISCORD_WEBHOOK_URL", None)
os.environ.pop("SOVEREIGN_LIVE_CONFIRM", None)

# sovereign_memory talks to Postgres; never let a test reach it.
sys.modules["sovereign_memory"] = types.SimpleNamespace(SovereignMemory=None)

from alpaca.common.enums import Sort  # noqa: E402
from alpaca.common.exceptions import APIError  # noqa: E402
from alpaca.common.utils import validate_uuid_id_param  # noqa: E402
from alpaca.data.enums import Adjustment  # noqa: E402
from alpaca.data.requests import StockLatestQuoteRequest, StockLatestTradeRequest  # noqa: E402
from alpaca.trading.enums import OrderSide, OrderStatus, QueryOrderStatus, TimeInForce  # noqa: E402
from alpaca.trading.models import Clock, Order, Position  # noqa: E402
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest  # noqa: E402

import risk_engine  # noqa: E402
import sovereign_config  # noqa: E402
import sovereign_execute as ex  # noqa: E402
from sovereign_config import RISK  # noqa: E402

ET = ZoneInfo("America/New_York")


# ==========================================================================
# network guard
# ==========================================================================

@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise RuntimeError("network access attempted in a test")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(requests.Session, "request", refuse)


# ==========================================================================
# alpaca-like errors
# ==========================================================================

def api_error(status: int, code: int, message: str, **extra) -> APIError:
    """An APIError shaped exactly as alpaca-py raises it: the body text is the
    server's JSON and status_code comes from the wrapped HTTPError."""
    http = requests.HTTPError(f"{status} Error", response=SimpleNamespace(status_code=status))
    return APIError(json.dumps({"code": code, "message": message, **extra}), http)


def not_found(what="order not found"):
    return api_error(404, 40410000, what)


def server_error():
    return api_error(500, 50010000, "internal server error")


def rate_limited():
    return api_error(429, 42910000, "rate limit exceeded")


OPEN_STATUSES = {"new", "partially_filled", "done_for_day", "accepted", "pending_new",
                 "accepted_for_bidding", "pending_cancel", "pending_replace", "pending_review",
                 "held", "calculated", "stopped", "suspended"}
CLOSED_STATUSES = {"filled", "canceled", "expired", "replaced", "rejected"}


# ==========================================================================
# fake broker
# ==========================================================================

@dataclass
class Plan:
    """What an order does as it is observed (get_order_by_id / by_client_id).
    kind: fill | never | cancel | reject | expire | partial | status.
    after: observations before the transition (0 = at birth).
    partial: fills `frac` at `after`, then `then` ('fill'|'cancel'|'expire'|None)
    after `then_after` more observations.
    status: moves to the (non-terminal) Alpaca status `status` at `after` and
    stays there (e.g. 'stopped', 'suspended', 'done_for_day', 'held')."""
    kind: str = "fill"
    after: int = 1
    price: float = None
    frac: float = 0.5
    then: str = None
    then_after: int = 1
    status: str = None


class _Rec:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeBroker:
    def __init__(self, prices=None, equity=100000.0, cash=90000.0, last_equity=None,
                 market_open=True):
        self.prices = dict(prices or {})
        self.equity, self.cash = equity, cash
        self.last_equity = equity if last_equity is None else last_equity
        self.market_open = market_open
        self.book = {}                 # symbol -> {"qty", "avg", "asset_id"}
        self.orders = {}               # str(uuid) -> _Rec
        self.plans = {}                # (symbol, side) or symbol -> Plan
        self.default_plan = Plan()
        self.fail = {}                 # method -> [exceptions raised before normal behaviour]
        self.submit_faults = {}        # symbol -> [("accept_then_raise"|"raise", exc)]
        self.close_faults = {}         # symbol -> [("accept_then_raise"|"raise", exc)]
        self.open_position_errors = {}  # symbol -> exc
        self.positions_script = []     # overrides for successive get_all_positions calls
        # How DELETE /orders/{id} behaves, per symbol. Real Alpaca: 204 and the
        # order goes pending_cancel -> canceled; 422 if it is no longer
        # cancelable (filled/canceled/...); 404 if unknown.
        #   "ok"          pending_cancel now, canceled at the next observation
        #   "slow"        pending_cancel now, canceled at the 3rd observation after
        #   "stuck"       pending_cancel and the venue never confirms
        #   "refuse"      422 'order is not cancelable', status unchanged
        #   "fills_first" the order fills in full, then the cancel 422s
        #   "raise"       500, nothing changes
        self.cancel_modes = {}
        self.null_price = set()        # symbols whose Position has current_price etc. = None
        self.raw_market_value = {}     # symbol -> the market_value string the server sends (e.g. "NaN")
        self.order_errors = {}         # order id -> exc raised by EVERY get_order_by_id (unreadable)
        self.submitted, self.close_calls, self.order_queries = [], [], []
        self.cancel_calls = []
        self.wash_rejections = []      # (symbol, side) of orders refused as potential wash trades
        self.calls = []

    # ---- helpers ---------------------------------------------------------
    def _maybe_fail(self, name):
        self.calls.append(name)
        q = self.fail.get(name)
        if q:
            raise q.pop(0)

    def hold(self, symbol, qty, avg, price=None):
        self.book[symbol] = {"qty": float(qty), "avg": float(avg), "asset_id": str(uuid.uuid4())}
        self.prices[symbol] = float(price if price is not None else avg)

    def _position(self, symbol):
        b = self.book[symbol]
        px = self.prices[symbol]
        qty, avg = b["qty"], b["avg"]
        if symbol in self.null_price:
            # alpaca-py types these Optional[str]; the server can send null
            return Position(asset_id=b["asset_id"], symbol=symbol, exchange="NASDAQ",
                            asset_class="us_equity", avg_entry_price=str(avg), qty=str(qty),
                            side="long", market_value=None, cost_basis=str(qty * avg),
                            unrealized_pl=None, unrealized_plpc=None, current_price=None,
                            qty_available=str(qty))
        return Position(asset_id=b["asset_id"], symbol=symbol, exchange="NASDAQ",
                        asset_class="us_equity", avg_entry_price=str(avg), qty=str(qty),
                        side="long", market_value=self.raw_market_value.get(symbol, str(qty * px)),
                        cost_basis=str(qty * avg),
                        unrealized_pl=str(qty * (px - avg)), unrealized_plpc=str(px / avg - 1),
                        current_price=str(px), qty_available=str(qty))

    def _plan(self, symbol, side):
        return self.plans.get((symbol, side)) or self.plans.get(symbol) or self.default_plan

    def _wash_check(self, symbol, side):
        """Alpaca 'Preventing Wash Trades': a market order while an opposite-side
        order for the symbol is open -- pending_cancel included -- is refused."""
        opposite = "sell" if side == "buy" else "buy"
        if any(r.symbol == symbol and r.side == opposite and r.status in OPEN_STATUSES
               for r in self.orders.values()):
            self.wash_rejections.append((symbol, side))
            raise api_error(403, 40310000, "potential wash trade detected. use complex orders",
                            reject_reason="opposite side market/stop order exists")

    def _new_order(self, symbol, side, notional=None, qty=None, client_order_id=None,
                   submitted_at=None, plan=None, tif="day"):
        now = submitted_at or datetime.now(timezone.utc)
        rec = _Rec(id=str(uuid.uuid4()), client_order_id=client_order_id or str(uuid.uuid4()),
                   tif=tif,
                   symbol=symbol, side=side, notional=notional, qty=qty, status="pending_new",
                   submitted_at=now, created_at=now, updated_at=now, filled_at=None,
                   canceled_at=None, filled_qty=0.0, filled_avg_price=None,
                   plan=plan or self._plan(symbol, side), reads=0, partial_done=False,
                   partial_at=None, replaced_by=None, cancel_requested=False,
                   cancel_stuck=False, cancel_after=0)
        self.orders[rec.id] = rec
        self._advance(rec)       # an after=0 plan transitions at birth
        return rec

    def _target_qty(self, rec, price):
        if rec.qty is not None:
            return float(rec.qty)
        return round(float(rec.notional) / price, 9)

    def _fill(self, rec, qty, price=None):
        price = float(price or rec.plan.price or self.prices[rec.symbol])
        prev_q, prev_px = rec.filled_qty, float(rec.filled_avg_price or 0)
        rec.filled_qty = prev_q + qty
        rec.filled_avg_price = (prev_q * prev_px + qty * price) / rec.filled_qty
        rec.updated_at = datetime.now(timezone.utc)
        b = self.book.get(rec.symbol)
        if rec.side == "buy":
            if b:
                tot = b["qty"] + qty
                b["avg"] = (b["qty"] * b["avg"] + qty * price) / tot
                b["qty"] = tot
            else:
                self.book[rec.symbol] = {"qty": qty, "avg": price, "asset_id": str(uuid.uuid4())}
            self.prices.setdefault(rec.symbol, price)
            self.cash -= qty * price
        else:
            if b:
                b["qty"] -= qty
                if b["qty"] <= 1e-9:
                    del self.book[rec.symbol]
            self.cash += qty * price

    def _finish(self, rec, status):
        rec.status = status
        now = datetime.now(timezone.utc)
        rec.updated_at = now
        if status == "filled":
            rec.filled_at = now
        if status == "canceled":
            rec.canceled_at = now

    def _advance(self, rec):
        if rec.status in CLOSED_STATUSES:
            return
        if rec.cancel_requested:
            if not rec.cancel_stuck and rec.status == "pending_cancel" and rec.reads >= rec.cancel_after:
                self._finish(rec, "canceled")
            return
        p = rec.plan
        if rec.status == "pending_new" and rec.reads >= 1:
            rec.status = "accepted"
        if p.kind == "never":
            return
        if rec.reads < p.after:
            return
        if p.kind == "status":
            rec.status = p.status
            return
        px = float(p.price or self.prices[rec.symbol])
        if p.kind == "fill":
            self._fill(rec, self._target_qty(rec, px) - rec.filled_qty, px)
            self._finish(rec, "filled")
        elif p.kind == "cancel":
            self._finish(rec, "canceled")
        elif p.kind == "reject":
            self._finish(rec, "rejected")
        elif p.kind == "expire":
            self._finish(rec, "expired")
        elif p.kind == "partial":
            if not rec.partial_done:
                self._fill(rec, round(self._target_qty(rec, px) * p.frac, 9), px)
                rec.status, rec.partial_done, rec.partial_at = "partially_filled", True, rec.reads
            elif p.then and rec.reads >= rec.partial_at + p.then_after:
                if p.then == "fill":
                    self._fill(rec, self._target_qty(rec, px) - rec.filled_qty, px)
                    self._finish(rec, "filled")
                elif p.then == "cancel":
                    self._finish(rec, "canceled")
                elif p.then == "expire":
                    self._finish(rec, "expired")

    def _model(self, rec) -> Order:
        lp, sp = getattr(rec, "limit_price", None), getattr(rec, "stop_price", None)
        otype = "stop_limit" if lp and sp else "limit" if lp else "stop" if sp else "market"
        return Order(
            id=rec.id, client_order_id=rec.client_order_id, created_at=rec.created_at,
            updated_at=rec.updated_at, submitted_at=rec.submitted_at, filled_at=rec.filled_at,
            canceled_at=rec.canceled_at, asset_id=str(uuid.uuid4()), symbol=rec.symbol,
            asset_class=getattr(rec, "asset_class", None) or "us_equity",
            notional=None if rec.notional is None else str(rec.notional),
            qty=None if rec.qty is None else str(rec.qty),
            filled_qty=str(rec.filled_qty),
            filled_avg_price=None if rec.filled_avg_price is None else str(round(rec.filled_avg_price, 4)),
            order_class="simple", order_type=otype, type=otype, side=rec.side,
            limit_price=None if lp is None else str(lp),
            stop_price=None if sp is None else str(sp),
            time_in_force=rec.tif, status=rec.status, extended_hours=False,
            replaced_by=rec.replaced_by)

    # ---- test controls (time passing between runs) -------------------------
    def fill_order(self, order_id, price=None):
        rec = self.orders[str(order_id)]
        px = float(price or self.prices[rec.symbol])
        self._fill(rec, self._target_qty(rec, px) - rec.filled_qty, px)
        self._finish(rec, "filled")

    def cancel_order(self, order_id):
        self._finish(self.orders[str(order_id)], "canceled")

    def age_order(self, order_id, minutes):
        rec = self.orders[str(order_id)]
        rec.submitted_at -= timedelta(minutes=minutes)
        rec.created_at = rec.submitted_at

    def seed_order(self, symbol, side, status, minutes_ago=0.0, price=None, qty=1.0,
                   client_order_id=None, filled=None, notional=None, plan=None,
                   limit_price=None, stop_price=None, asset_class=None):
        """An order already at the broker. `filled` sets filled_qty explicitly
        (e.g. an expired order that sold 6 of 10 before it expired)."""
        rec = self._new_order(symbol, side, qty=None if notional else qty, notional=notional,
                              client_order_id=client_order_id,
                              submitted_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
                              plan=plan or Plan("never"))
        rec.status = status
        rec.limit_price, rec.stop_price, rec.asset_class = limit_price, stop_price, asset_class
        if filled is not None:
            rec.filled_qty = float(filled)
            rec.filled_avg_price = float(price or self.prices.get(symbol, 100.0)) if filled else None
        elif status in ("filled", "partially_filled"):
            rec.filled_qty = qty if status == "filled" else qty / 2
            rec.filled_avg_price = float(price or self.prices.get(symbol, 100.0))
        return rec

    def replace_order(self, order_id, plan=None):
        """PATCH /orders/{id}: a new order replaces the old one, which goes
        'replaced' and carries replaced_by (alpaca-py Order.replaced_by)."""
        old = self.orders[str(order_id)]
        new = self._new_order(old.symbol, old.side, notional=old.notional, qty=old.qty,
                              plan=plan or Plan("never"))
        old.status, old.replaced_by = "replaced", new.id
        old.updated_at = datetime.now(timezone.utc)
        return new

    def orders_for(self, symbol, side=None):
        return [r for r in self.orders.values()
                if r.symbol == symbol and (side is None or r.side == side)]

    # ---- the TradingClient surface -----------------------------------------
    def get_account(self):
        self._maybe_fail("get_account")
        return SimpleNamespace(equity=str(self.equity), cash=str(self.cash),
                               last_equity=str(self.last_equity))

    def get_clock(self):
        self._maybe_fail("get_clock")
        now = datetime.now(timezone.utc)
        return Clock(timestamp=now, is_open=self.market_open,
                     next_open=now + timedelta(hours=18), next_close=now + timedelta(hours=6))

    def get_all_positions(self):
        self._maybe_fail("get_all_positions")
        if self.positions_script:
            override = self.positions_script.pop(0)
            if override is not None:
                return list(override)
        return [self._position(s) for s in self.book]

    def get_open_position(self, symbol_or_asset_id):
        self._maybe_fail("get_open_position")
        if symbol_or_asset_id in self.open_position_errors:
            raise self.open_position_errors[symbol_or_asset_id]
        if symbol_or_asset_id not in self.book:
            raise not_found("position does not exist")
        return self._position(symbol_or_asset_id)

    def submit_order(self, order_data):
        self._maybe_fail("submit_order")
        assert isinstance(order_data, MarketOrderRequest), "execute must use real alpaca requests"
        self.submitted.append(order_data)
        sym = order_data.symbol
        coid = order_data.client_order_id
        faults = self.submit_faults.get(sym) or []
        fault = faults.pop(0) if faults else None
        if fault and fault[0] == "raise":
            raise fault[1]
        if coid and any(r.client_order_id == coid for r in self.orders.values()):
            raise api_error(422, 40010001, "client_order_id must be unique")
        side = order_data.side.value if hasattr(order_data.side, "value") else str(order_data.side)
        tif = getattr(order_data.time_in_force, "value", order_data.time_in_force)
        fractional = order_data.notional is not None or (
            order_data.qty is not None and float(order_data.qty) != int(float(order_data.qty)))
        if fractional and tif != "day":
            raise api_error(422, 42210000, "fractional orders must be DAY orders")
        if order_data.notional is not None and round(float(order_data.notional), 2) != float(order_data.notional):
            raise api_error(422, 42210000, "notional must be limited to 2 decimal places")
        self._wash_check(sym, side)
        rec = self._new_order(sym, side, notional=order_data.notional, qty=order_data.qty,
                              client_order_id=coid, tif=tif)
        if fault and fault[0] == "accept_then_raise":
            raise fault[1]
        return self._model(rec)

    def get_order_by_id(self, order_id, filter=None):
        self._maybe_fail("get_order_by_id")
        oid = str(validate_uuid_id_param(order_id, "order_id"))
        if oid in self.order_errors:
            raise self.order_errors[oid]
        rec = self.orders.get(oid)
        if rec is None:
            raise not_found()
        rec.reads += 1
        self._advance(rec)
        return self._model(rec)

    def get_order_by_client_id(self, client_id):
        self._maybe_fail("get_order_by_client_id")
        hits = [r for r in self.orders.values() if r.client_order_id == client_id]
        if not hits:
            raise not_found(f"order not found for {client_id}")
        rec = hits[0]
        rec.reads += 1
        self._advance(rec)
        return self._model(rec)

    def cancel_order_by_id(self, order_id):
        self._maybe_fail("cancel_order_by_id")
        oid = str(validate_uuid_id_param(order_id, "order_id"))
        self.cancel_calls.append(oid)
        rec = self.orders.get(oid)
        if rec is None:
            raise not_found()
        mode = self.cancel_modes.get(rec.symbol, "ok")
        if mode == "raise":
            raise server_error()
        if mode == "fills_first" and rec.status not in CLOSED_STATUSES:
            self.fill_order(rec.id)
        if rec.status in CLOSED_STATUSES or mode == "refuse":
            raise api_error(422, 42210000, "order is not cancelable")
        rec.status, rec.cancel_requested = "pending_cancel", True
        rec.cancel_stuck = mode == "stuck"
        rec.cancel_after = rec.reads + (3 if mode == "slow" else 1)
        rec.updated_at = datetime.now(timezone.utc)
        return None

    def get_orders(self, filter=None):
        self._maybe_fail("get_orders")
        f = filter if filter is not None else GetOrdersRequest()
        self.order_queries.append(f)
        status = f.status or QueryOrderStatus.OPEN          # server default
        limit = f.limit or 50
        assert limit <= 500, "Alpaca caps limit at 500"
        direction = f.direction or Sort.DESC

        def aware(d):
            return d if d is None or d.tzinfo else d.replace(tzinfo=timezone.utc)
        after, until = aware(f.after), aware(f.until)
        out = []
        for r in self.orders.values():
            if status == QueryOrderStatus.OPEN and r.status not in OPEN_STATUSES:
                continue
            if status == QueryOrderStatus.CLOSED and r.status not in CLOSED_STATUSES:
                continue
            if f.symbols and r.symbol not in f.symbols:
                continue
            if f.side and r.side != (f.side.value if hasattr(f.side, "value") else f.side):
                continue
            if after and not r.submitted_at > after:
                continue
            if until and not r.submitted_at <= until:
                continue
            out.append(r)
        out.sort(key=lambda r: r.submitted_at, reverse=(direction == Sort.DESC))
        return [self._model(r) for r in out[:limit]]

    def close_position(self, symbol_or_asset_id, close_options=None):
        self._maybe_fail("close_position")
        sym = symbol_or_asset_id
        self.close_calls.append(sym)
        faults = self.close_faults.get(sym) or []
        fault = faults.pop(0) if faults else None
        if fault and fault[0] == "raise":
            raise fault[1]
        if sym not in self.book:
            raise not_found("position does not exist")
        qty = self.book[sym]["qty"]
        working = sum(r.qty or 0 for r in self.orders.values()
                      if r.symbol == sym and r.side == "sell" and r.status in OPEN_STATUSES)
        if working >= qty - 1e-9:
            raise api_error(403, 40310000, f"insufficient qty available for order "
                                           f"(requested: {qty}, available: 0)",
                            held_for_orders=str(qty), available="0")
        self._wash_check(sym, "sell")
        rec = self._new_order(sym, "sell", qty=qty)
        if fault and fault[0] == "accept_then_raise":
            raise fault[1]
        return self._model(rec)


class FakeClock:
    """Replaces the `time` module inside sovereign_execute: sleep advances a
    monotonic clock instantly, so polling loops run for their real number of
    iterations without real waiting."""
    def __init__(self):
        self.t = 1000.0
        self.sleeps = []

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s

    def time(self):
        return self.t


# ==========================================================================
# fixtures and helpers
# ==========================================================================

@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Point every state/results path at a temp dir, fake the clock, and route
    alerts through the REAL sovereign_alerts.alert() with only its transport
    replaced. Dedupe is therefore the real one: the per-(title, level) key in a
    per-day alerts_sent.json (sandboxed). `alerts` holds what was DELIVERED;
    `alert_calls` holds every call, including the ones dedupe suppressed."""
    import sovereign_alerts
    state, results = tmp_path / "state", tmp_path / "results"
    state.mkdir(), results.mkdir()
    monkeypatch.setattr(risk_engine, "POSITIONS_STATE", state / "positions_state.json")
    monkeypatch.setattr(risk_engine, "HALT_FILE", state / "halt.json")
    monkeypatch.setattr(risk_engine, "TRADE_LOG", state / "trade_log.jsonl")
    monkeypatch.setattr(ex, "TRADE_LOG", state / "trade_log.jsonl")
    monkeypatch.setattr(ex, "ACCOUNT_SNAPSHOT", state / "account_snapshot.json")
    monkeypatch.setattr(ex, "LOCK_FILE", state / ".sovereign.lock")
    monkeypatch.setattr(ex, "STATE_DIR", state)
    monkeypatch.setattr(ex, "RESULTS_DIR", results)
    clock = FakeClock()
    monkeypatch.setattr(ex, "time", clock)
    alerts, alert_calls = [], []
    monkeypatch.setattr(sovereign_alerts, "SENT_FILE", state / "alerts_sent.json")

    def deliver(title, lines, level):
        last = alert_calls[-1] if alert_calls and alert_calls[-1][0] == title else (title, lines, {"level": level})
        alerts.append(last)
        return True
    monkeypatch.setattr(sovereign_alerts, "_send_discord", deliver)
    monkeypatch.setattr(sovereign_alerts, "_send_email", lambda title, lines: False)

    def stub(title, lines, **kw):
        alert_calls.append((title, lines, kw))
        sovereign_alerts.alert(title, lines, **kw)
    monkeypatch.setattr(ex, "alert", stub)
    monkeypatch.setattr(risk_engine, "avg_correlation_with_book", lambda c, h: 0.0)
    return SimpleNamespace(state=state, results=results, alerts=alerts, alert_calls=alert_calls,
                           clock=clock, positions=state / "positions_state.json",
                           sent_file=state / "alerts_sent.json")


def use(monkeypatch, fb):
    monkeypatch.setattr(ex, "_trading_client", lambda: (fb, True))
    return fb


def trades(sb):
    p = sb.state / "trade_log.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []


def sells(sb, ticker=None):
    return [t for t in trades(sb) if t["action"] == "sell" and (ticker is None or t["ticker"] == ticker)]


def buys(sb, ticker=None):
    return [t for t in trades(sb) if t["action"] == "buy" and (ticker is None or t["ticker"] == ticker)]


def state_now(sb):
    return json.loads(sb.positions.read_text()) if sb.positions.exists() else {}


def alerts_matching(sb, text):
    return [a for a in sb.alerts if text.lower() in a[0].lower()]


def write_run(results, run_id, signals, theses, with_pointer=True, halted=None):
    (results / f"signals_{run_id}.json").write_text(json.dumps(
        {"run_id": run_id, "timestamp": datetime.now().isoformat(), "signals": signals}))
    scan = {"run_id": run_id, "macro": {"exposure_multiplier": 1.0}, "theses": theses}
    if with_pointer:
        scan["signals_file"] = f"signals_{run_id}.json"
    if halted:
        scan["halted"] = halted
    (results / f"scan_{run_id}.json").write_text(json.dumps(scan))


def sig(score=0.4, conviction="medium", flags=()):
    return {"score": score, "conviction": conviction, "risk_flags": list(flags), "components": []}


def buy_thesis(ticker, entry=100.0, stop=92.0, target=115.0):
    return {"ticker": ticker, "direction": "buy", "conviction": "medium",
            "entry_price": entry, "stop_loss": stop, "target": target, "reasoning": "t"}


def now_id(minutes_ago=0):
    return (datetime.now() - timedelta(minutes=minutes_ago)).strftime("%Y%m%d_%H%M%S")


def track(sym, entry=100.0, stop=92.0, target=115.0, **kw):
    risk_engine.record_entry(sym, entry, 1000.0, stop, target, reason="t", **kw)


def append_log(sb, *recs):
    with open(sb.state / "trade_log.jsonl", "a") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")


class FrozenET(datetime):
    """datetime whose now(ET) is pinned; every other call is real."""
    pinned = None

    @classmethod
    def now(cls, tz=None):
        if tz is not None and getattr(tz, "key", None) == "America/New_York":
            return cls.pinned
        return datetime.now(tz)


def pin_exchange_time(monkeypatch, when: datetime):
    FrozenET.pinned = when
    monkeypatch.setattr(ex, "datetime", FrozenET)


WEDNESDAY_11AM = datetime(2026, 9, 23, 11, 0, tzinfo=ET)
SATURDAY_11AM = datetime(2026, 9, 26, 11, 0, tzinfo=ET)


# ==========================================================================
# 0. the fake broker behaves like alpaca-py (F14)
# ==========================================================================

def test_fake_get_orders_defaults_to_open_like_the_server():
    fb = FakeBroker(prices={"AAA": 10.0})
    done = fb.seed_order("AAA", "sell", "filled", minutes_ago=1, price=10.0)
    live = fb.seed_order("AAA", "sell", "accepted", minutes_ago=2)
    assert [str(o.id) for o in fb.get_orders()] == [live.id]
    assert [str(o.id) for o in fb.get_orders(GetOrdersRequest())] == [live.id]
    assert {str(o.id) for o in fb.get_orders(GetOrdersRequest(status=QueryOrderStatus.ALL))} == {done.id, live.id}
    assert [str(o.id) for o in fb.get_orders(GetOrdersRequest(status=QueryOrderStatus.CLOSED))] == [done.id]


def test_fake_get_orders_honours_symbols_window_limit_direction():
    fb = FakeBroker(prices={"AAA": 10.0, "BBB": 5.0})
    old = fb.seed_order("AAA", "sell", "filled", minutes_ago=30)
    mid = fb.seed_order("AAA", "sell", "filled", minutes_ago=10)
    new = fb.seed_order("AAA", "sell", "filled", minutes_ago=1)
    fb.seed_order("BBB", "sell", "filled", minutes_ago=1)
    now = datetime.now(timezone.utc)
    q = GetOrdersRequest(status=QueryOrderStatus.ALL, symbols=["AAA"])
    assert [str(o.id) for o in fb.get_orders(q)] == [new.id, mid.id, old.id]  # desc default
    q = GetOrdersRequest(status=QueryOrderStatus.ALL, symbols=["AAA"], direction=Sort.ASC, limit=2)
    assert [str(o.id) for o in fb.get_orders(q)] == [old.id, mid.id]
    q = GetOrdersRequest(status=QueryOrderStatus.ALL, symbols=["AAA"],
                         after=now - timedelta(minutes=15), until=now - timedelta(minutes=5))
    assert [str(o.id) for o in fb.get_orders(q)] == [mid.id]


def test_fake_orders_look_like_real_orders():
    fb = FakeBroker(prices={"AAA": 10.0})
    o = fb.submit_order(MarketOrderRequest(symbol="AAA", notional=100.0, client_order_id="c1",
                                           side=OrderSide.BUY, time_in_force=TimeInForce.DAY))
    assert isinstance(o, Order) and isinstance(o.id, uuid.UUID)
    assert o.status == OrderStatus.PENDING_NEW and o.filled_avg_price is None
    assert o.submitted_at.tzinfo is not None
    o2 = fb.get_order_by_id(o.id)
    assert o2.status == OrderStatus.FILLED and o2.filled_avg_price == "10.0"
    assert isinstance(o2.filled_qty, str)
    with pytest.raises(APIError) as e:
        fb.submit_order(MarketOrderRequest(symbol="AAA", notional=100.0, client_order_id="c1",
                                           side=OrderSide.BUY, time_in_force=TimeInForce.DAY))
    assert e.value.status_code == 422


@pytest.mark.parametrize("call", [
    lambda fb: fb.close_position("NOPE"),
    lambda fb: fb.get_open_position("NOPE"),
    lambda fb: fb.get_order_by_client_id("sov-nope"),
    lambda fb: fb.get_order_by_id(uuid.uuid4()),
])
def test_fake_absent_things_are_404s_the_code_recognises(call):
    with pytest.raises(APIError) as e:
        call(FakeBroker())
    assert e.value.status_code == 404
    assert ex._is_not_found(e.value)


def test_a_500_is_not_mistaken_for_not_found():
    assert not ex._is_not_found(server_error())
    assert not ex._is_not_found(rate_limited())


# ==========================================================================
# 1. the stop rule (kept from the previous file)
# ==========================================================================

def test_tight_llm_stop_is_widened_to_the_rule():
    stop, note = risk_engine.enforce_stop_rule(50.00, 49.80)
    assert stop == round(50.00 * 0.92, 2) and note


def test_wider_llm_stop_is_kept():
    stop, note = risk_engine.enforce_stop_rule(200.00, 174.00)
    assert stop == 174.00 and note is None


def test_stop_at_or_above_entry_is_replaced():
    assert risk_engine.enforce_stop_rule(100.0, 100.0)[0] == 92.0
    assert risk_engine.enforce_stop_rule(100.0, 0.0)[0] == 92.0


def test_atr_rule_picks_the_wider_stop():
    assert risk_engine.rule_stop(100.0, atr=6.0) == 88.0
    assert risk_engine.rule_stop(100.0, atr=1.0) == 92.0


# ==========================================================================
# 2. thesis generation (kept) + sdk backend without a key (F15)
# ==========================================================================

@pytest.fixture
def pipeline():
    import sovereign_pipeline as sp
    return sp


def _stock(sp, price=50.00, atr=0.0):
    return sp.StockData(ticker="TGHT", current_price=price, atr_14=atr)


def _cli_returns(monkeypatch, stdout="", rc=0, stderr=""):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)
    monkeypatch.setattr("subprocess.run", fake_run)
    return calls


def test_cli_backend_never_constructs_the_sdk(monkeypatch, pipeline):
    sp = pipeline
    monkeypatch.setenv("SOVEREIGN_THESIS_BACKEND", "cli")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    constructed = []
    monkeypatch.setitem(sys.modules, "anthropic",
                        SimpleNamespace(Anthropic=lambda *a, **k: constructed.append(1) or SimpleNamespace()))
    calls = _cli_returns(monkeypatch, stdout=json.dumps(
        {"direction": "hold", "conviction": "none", "entry_price": 79.3, "stop_loss": 70, "target": 90}))
    th = sp.generate_thesis("TGHT", _stock(sp), sp.MarketContext())
    assert not constructed, "SDK client constructed under backend=cli"
    assert th is not None and calls, "CLI was not used"


def test_sdk_backend_without_a_key_returns_none_and_never_falls_back_to_cli(monkeypatch, pipeline):
    sp = pipeline
    monkeypatch.setenv("SOVEREIGN_THESIS_BACKEND", "sdk")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    calls = _cli_returns(monkeypatch, stdout=json.dumps({"direction": "buy", "conviction": "high"}))
    assert sp.generate_thesis("TGHT", _stock(sp), sp.MarketContext()) is None
    assert calls == [], "backend=sdk must not silently use the CLI"


def test_llm_stop_tighter_than_rule_is_widened_in_the_thesis(monkeypatch, pipeline):
    sp = pipeline
    monkeypatch.setenv("SOVEREIGN_THESIS_BACKEND", "cli")
    _cli_returns(monkeypatch, stdout=json.dumps(
        {"direction": "buy", "conviction": "medium", "entry_price": 50.00,
         "stop_loss": 49.80, "target": 90}))
    th = sp.generate_thesis("TGHT", _stock(sp), sp.MarketContext())
    assert th.stop_loss == round(50.00 * 0.92, 2)
    assert any("[code]" in r for r in th.risk_factors)


def test_non_numeric_prices_do_not_abort(monkeypatch, pipeline):
    sp = pipeline
    monkeypatch.setenv("SOVEREIGN_THESIS_BACKEND", "cli")
    _cli_returns(monkeypatch, stdout=json.dumps(
        {"direction": "buy", "conviction": "low", "entry_price": "$50.00",
         "stop_loss": "n/a", "target": "about 90", "position_size_pct": "10%"}))
    th = sp.generate_thesis("TGHT", _stock(sp), sp.MarketContext())
    assert th.entry_price == 50.00 and th.stop_loss == round(50.00 * 0.92, 2)
    assert th.target == round(50.00 * 1.15, 2)


def test_cli_failure_logs_stdout(monkeypatch, pipeline, caplog):
    sp = pipeline
    monkeypatch.setenv("SOVEREIGN_THESIS_BACKEND", "cli")
    _cli_returns(monkeypatch, stdout="Claude usage limit reached", rc=1, stderr="")
    with caplog.at_level("ERROR"):
        assert sp.generate_thesis("TGHT", _stock(sp), sp.MarketContext()) is None
    assert "usage limit" in caplog.text


def test_snapshot_is_saved_after_both_gates():
    src = (REPO / "sovereign_pipeline.py").read_text()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "scan_opportunities")
    body = ast.get_source_segment(src, fn)
    snap = body.index("save_snapshot(")
    assert body.index("Sector headwind gate") < snap
    assert body.index("Sector exhaustion gate") < snap


def test_snapshot_persists_post_gate_conviction(tmp_path, monkeypatch):
    import signal_aggregator as sa
    monkeypatch.setattr(sa, "RESULTS_DIR", tmp_path)
    s = SimpleNamespace(score=0.4, conviction="low", risk_flags=["sector exhaustion risk"],
                        components=[])
    path = sa.save_snapshot({"GATE": s}, run_id="20260101_063000")
    got = json.loads(path.read_text())
    assert got["run_id"] == "20260101_063000"
    assert got["signals"]["GATE"]["conviction"] == "low"
    assert got["signals"]["GATE"]["risk_flags"] == ["sector exhaustion risk"]


# ==========================================================================
# 3. which run execute acts on (kept) + scan_opportunities end to end (F15)
# ==========================================================================

def test_scan_is_paired_with_its_own_snapshot(sandbox):
    rid = now_id(5)
    write_run(sandbox.results, rid, {"AAA": sig()}, [buy_thesis("AAA")])
    (sandbox.results / f"signals_{now_id(1)}.json").write_text(json.dumps(
        {"signals": {"ZZZ": sig()}, "timestamp": datetime.now().isoformat()}))
    scan, sigs, why = ex._latest_run()
    assert why is None and "AAA" in sigs["signals"] and "ZZZ" not in sigs["signals"]


def test_stale_scan_is_refused(sandbox):
    write_run(sandbox.results, now_id(60 * 3), {"AAA": sig()}, [buy_thesis("AAA")])
    why = ex._latest_run()[2]
    assert why and "old" in why


def test_named_snapshot_missing_is_refused(sandbox):
    rid = now_id(5)
    write_run(sandbox.results, rid, {"AAA": sig()}, [buy_thesis("AAA")])
    (sandbox.results / f"signals_{rid}.json").unlink()
    assert "missing" in ex._latest_run()[2]


def test_crisis_scan_blocks_execution(sandbox):
    write_run(sandbox.results, now_id(60), {"AAA": sig()}, [buy_thesis("AAA")])
    write_run(sandbox.results, now_id(5), {}, [], halted="CRISIS regime — no new positions")
    assert "CRISIS" in ex._latest_run()[2]


def test_legacy_scan_without_pointer_needs_a_close_snapshot(sandbox):
    rid = now_id(5)
    write_run(sandbox.results, rid, {"AAA": sig()}, [buy_thesis("AAA")], with_pointer=False)
    assert ex._latest_run()[2] is None
    (sandbox.results / f"signals_{rid}.json").unlink()
    (sandbox.results / f"signals_{now_id(125)}.json").write_text(json.dumps({"signals": {}}))
    assert "older than the scan" in ex._latest_run()[2]


def _quiet_scan_env(monkeypatch, sandbox, regime):
    import congress_scraper
    import correlation_breaks
    import macro_regime
    import member_scoring
    import signal_aggregator
    import sovereign_alerts
    import sovereign_pipeline as sp
    monkeypatch.setattr(sp, "RESULTS_DIR", sandbox.results)
    monkeypatch.setattr(signal_aggregator, "RESULTS_DIR", sandbox.results)
    monkeypatch.setattr(sp, "load_opportunity_candidates", lambda *a, **k: [])
    monkeypatch.setattr(congress_scraper, "load_existing_transactions", lambda *a, **k: [])
    monkeypatch.setattr(member_scoring, "load_member_weights", lambda *a, **k: {})
    monkeypatch.setattr(correlation_breaks, "detect_breaks", lambda *a, **k: {})
    monkeypatch.setattr(sp, "get_macro_context", lambda: sp.MarketContext(market_open=True))
    monkeypatch.setattr(macro_regime, "get_regime", lambda *a, **k: regime)
    monkeypatch.setitem(sys.modules, "options_flow", SimpleNamespace(get_flow=lambda t: {}))
    monkeypatch.setitem(sys.modules, "earnings_radar", SimpleNamespace(get_radar=lambda t: {}))
    monkeypatch.setattr(sovereign_alerts, "alert_high_conviction", lambda *a, **k: None)
    return sp


def test_crisis_scan_writes_an_empty_halted_scan_that_blocks_execute(sandbox, monkeypatch):
    """F15: a CRISIS scan must supersede an older actionable scan."""
    sp = _quiet_scan_env(monkeypatch, sandbox,
                         {"regime": "CRISIS", "score": -1.0, "exposure_multiplier": 0.0})
    write_run(sandbox.results, now_id(60), {"AAA": sig()}, [buy_thesis("AAA")])
    assert sp.scan_opportunities(watchlist=["AAA"]) == []
    scan, sigs, why = ex._latest_run()
    assert why and "CRISIS" in why
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 100.0}))
    ex.execute()
    assert fb.submitted == []


def test_scan_names_its_own_snapshot_and_execute_follows_the_pointer(sandbox, monkeypatch):
    """F15: signals_file pointer, end to end through scan_opportunities."""
    sp = _quiet_scan_env(monkeypatch, sandbox,
                         {"regime": "NORMAL", "score": 0.5, "exposure_multiplier": 1.0})
    import signal_aggregator as sa
    monkeypatch.setattr(sp, "get_stock_data",
                        lambda t, market_open=False: sp.StockData(ticker=t, current_price=100.0))
    monkeypatch.setattr(sa, "aggregate", lambda stocks, *a, **k: {
        t: sa.CompositeSignal(ticker=t, score=0.4, conviction="medium") for t in stocks})
    monkeypatch.setattr(sp, "generate_thesis", lambda t, *a, **k: sp.Thesis(
        ticker=t, direction="buy", conviction="medium", entry_price=100.0, stop_loss=92.0,
        target=115.0, reasoning="t"))
    sp.scan_opportunities(watchlist=["AAA"])
    scan_file = sorted(sandbox.results.glob("scan_*.json"))[-1]
    scan = json.loads(scan_file.read_text())
    assert scan["signals_file"] == f"signals_{scan['run_id']}.json"
    assert scan_file.name == f"scan_{scan['run_id']}.json"
    assert (sandbox.results / scan["signals_file"]).exists()
    # a newer stray snapshot (a later scan that died) must not be paired with it
    stray = (datetime.strptime(scan["run_id"], "%Y%m%d_%H%M%S") + timedelta(seconds=30))
    (sandbox.results / f"signals_{stray.strftime('%Y%m%d_%H%M%S')}.json").write_text(
        json.dumps({"signals": {"ZZZ": sig()}}))
    got_scan, sigs, why = ex._latest_run()
    assert why is None and set(sigs["signals"]) == {"AAA"}


# ==========================================================================
# 4. safety rails
# ==========================================================================

def test_live_account_refused_without_confirmation(monkeypatch):
    monkeypatch.setenv("ALPACA_PAPER", "false")
    monkeypatch.delenv("SOVEREIGN_LIVE_CONFIRM", raising=False)
    constructed = []
    import alpaca.trading.client as tc
    monkeypatch.setattr(tc, "TradingClient", lambda *a, **k: constructed.append(1))
    with pytest.raises(RuntimeError, match="Refusing"):
        ex._trading_client()
    assert not constructed


def test_every_trading_call_carries_an_http_timeout(monkeypatch):
    """F6: alpaca-py sets no requests timeout of its own."""
    seen = []

    def recorder(self, method, url, **kw):
        seen.append((method, kw.get("timeout")))
        raise requests.ConnectionError("blocked in tests")
    monkeypatch.setattr(requests.Session, "request", recorder)
    client, paper = ex._trading_client()
    assert paper is True
    for call in (client.get_clock, client.get_all_positions,
                 lambda: client.close_position("AAA"),
                 lambda: client.submit_order(MarketOrderRequest(
                     symbol="AAA", notional=5.0, client_order_id="sov-t-AAA",
                     side=OrderSide.BUY, time_in_force=TimeInForce.DAY))):
        with pytest.raises(requests.ConnectionError):
            call()
    assert [m for m, _ in seen] == ["GET", "GET", "DELETE", "POST"]
    assert all(t == ex.HTTP_TIMEOUT_S for _, t in seen), seen


def test_overlapping_run_is_refused(sandbox, monkeypatch):
    """F6: one run at a time; a second run must not touch the broker."""
    fb = FakeBroker(prices={"AAA": 80.0})
    fb.hold("AAA", 10, 100.0, 80.0)
    track("AAA")
    touched = []
    monkeypatch.setattr(ex, "_trading_client", lambda: touched.append(1) or (fb, True))
    with open(ex.LOCK_FILE, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        ex.manage()
        ex.execute()
        assert not touched and fb.close_calls == []
    ex.manage()
    assert touched and fb.close_calls == ["AAA"]


def test_circuit_breaker_blocks_entries(sandbox, monkeypatch):
    write_run(sandbox.results, now_id(5), {"AAA": sig()}, [buy_thesis("AAA")])
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 100.0}, equity=94000.0, last_equity=100000.0))
    ex.execute()
    assert fb.submitted == []
    assert json.loads(risk_engine.HALT_FILE.read_text())["date"] == datetime.now().strftime("%Y-%m-%d")
    assert alerts_matching(sandbox, "circuit breaker")


def test_circuit_breaker_does_not_stop_stop_losses(sandbox, monkeypatch):
    track("AAA")
    fb = use(monkeypatch, FakeBroker(equity=94000.0, last_equity=100000.0))
    fb.hold("AAA", 10, 100.0, 85.0)
    ex.manage()
    assert fb.close_calls == ["AAA"] and len(sells(sandbox, "AAA")) == 1


def test_no_entries_when_market_closed(sandbox, monkeypatch):
    write_run(sandbox.results, now_id(5), {"AAA": sig()}, [buy_thesis("AAA")])
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 100.0}, market_open=False))
    ex.execute()
    assert fb.submitted == []


def test_no_entries_when_clock_unreadable_even_in_exchange_hours(sandbox, monkeypatch):
    """Entries need a CONFIRMED open market; the exchange-hours guess is for stops."""
    write_run(sandbox.results, now_id(5), {"AAA": sig()}, [buy_thesis("AAA")])
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 100.0}))
    fb.fail["get_clock"] = [server_error() for _ in range(5)]
    pin_exchange_time(monkeypatch, WEDNESDAY_11AM)
    ex.execute()
    assert fb.submitted == []


# ==========================================================================
# 5. execute
# ==========================================================================

def test_buys_in_one_run_see_each_other(sandbox, monkeypatch):
    monkeypatch.setitem(RISK, "max_positions", 1)
    write_run(sandbox.results, now_id(5), {"AAA": sig(0.5), "BBB": sig(0.4)},
              [buy_thesis("AAA"), buy_thesis("BBB")])
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 100.0, "BBB": 100.0}))
    ex.execute()
    assert [r.symbol for r in fb.submitted] == ["AAA"]


def test_sector_cap_counts_buys_made_earlier_in_the_run(sandbox, monkeypatch):
    """F15: NVDA and AMD share a sector; the second buy gets only the room left."""
    monkeypatch.setitem(RISK, "sector_cap_pct", 0.20)
    write_run(sandbox.results, now_id(5), {"NVDA": sig(0.5), "AMD": sig(0.4)},
              [buy_thesis("NVDA"), buy_thesis("AMD")])
    fb = use(monkeypatch, FakeBroker(prices={"NVDA": 100.0, "AMD": 100.0}))
    ex.execute()
    assert [(r.symbol, r.notional) for r in fb.submitted] == [("NVDA", 15000.0), ("AMD", 5000.0)]


def test_entry_is_the_fill_not_the_thesis(sandbox, monkeypatch):
    write_run(sandbox.results, now_id(5), {"FILL": sig()},
              [buy_thesis("FILL", entry=200.00, stop=174.00, target=230)])
    use(monkeypatch, FakeBroker(prices={"FILL": 204.00}))
    ex.execute()
    st = state_now(sandbox)["FILL"]
    assert st["entry"] == 204.00 and st["thesis_entry"] == 200.00 and st["entry_source"] == "fill"
    assert st["stop"] == round(204.00 * (1 - 0.13), 2)   # the thesis's 13% distance, from the fill
    assert "pending_buy" not in st
    assert buys(sandbox, "FILL")[0]["fill_price"] == 204.00


def test_target_at_or_below_the_fill_is_lifted(sandbox, monkeypatch):
    """F15: a target the fill has already passed is not a target."""
    write_run(sandbox.results, now_id(5), {"GAP": sig()},
              [buy_thesis("GAP", entry=100.0, stop=92.0, target=101.0)])
    use(monkeypatch, FakeBroker(prices={"GAP": 105.0}))
    ex.execute()
    st = state_now(sandbox)["GAP"]
    assert st["entry"] == 105.0 and st["target"] == round(105.0 * 1.15, 2)


def test_execute_floors_a_tight_stop_from_an_old_scan(sandbox, monkeypatch):
    write_run(sandbox.results, now_id(5), {"TGHT": sig()},
              [buy_thesis("TGHT", entry=50.00, stop=49.80, target=90)])
    use(monkeypatch, FakeBroker(prices={"TGHT": 50.00}))
    ex.execute()
    assert state_now(sandbox)["TGHT"]["stop"] == round(50.00 * 0.92, 2)


def test_buy_carries_a_client_order_id_naming_the_run(sandbox, monkeypatch):
    rid = now_id(5)
    write_run(sandbox.results, rid, {"AAA": sig()}, [buy_thesis("AAA")])
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 100.0}))
    ex.execute()
    coid = fb.submitted[0].client_order_id
    assert coid and coid.startswith("sov-") and rid in coid and coid.endswith("AAA")


def test_fill_is_confirmed_by_polling_not_assumed(sandbox, monkeypatch):
    """F14: orders are born pending_new; the fill arrives on the 3rd poll."""
    write_run(sandbox.results, now_id(5), {"AAA": sig()}, [buy_thesis("AAA")])
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 101.0}))
    fb.plans[("AAA", "buy")] = Plan("fill", after=3)
    ex.execute()
    rec = fb.orders_for("AAA", "buy")[0]
    assert rec.reads == 3
    st = state_now(sandbox)["AAA"]
    assert st["entry"] == 101.0 and "pending_buy" not in st


def test_fill_wait_is_read_at_call_time(sandbox, monkeypatch):
    """F14: FILL_WAIT_S was bound at def time, so tests could not shorten it."""
    fb = FakeBroker(prices={"AAA": 10.0})
    fb.plans["AAA"] = Plan("fill", after=10)
    o1 = fb.submit_order(MarketOrderRequest(symbol="AAA", notional=10.0, client_order_id="a",
                                            side=OrderSide.BUY, time_in_force=TimeInForce.DAY))
    monkeypatch.setattr(ex, "FILL_WAIT_S", 3)
    assert ex._order_outcome(fb, o1.id).state == "open"
    monkeypatch.setattr(ex, "FILL_WAIT_S", 60)
    state, px, qty, replaced_by = ex._order_outcome(fb, o1.id)
    assert state == "filled" and px == 10.0 and qty == pytest.approx(1.0) and replaced_by is None


def test_order_outcome_classifies_dead_orders(sandbox):
    fb = FakeBroker(prices={"AAA": 10.0})
    for kind in ("cancel", "reject", "expire"):
        fb.plans["AAA"] = Plan(kind, after=1)
        o = fb.submit_order(MarketOrderRequest(symbol="AAA", notional=10.0,
                                               client_order_id=f"c-{kind}", side=OrderSide.BUY,
                                               time_in_force=TimeInForce.DAY))
        assert ex._order_outcome(fb, o.id)[0] == "dead", kind


def test_buy_that_raised_but_was_accepted_is_recorded(sandbox, monkeypatch):
    """A 504'd POST that the broker accepted; alpaca-py's resend got a 422."""
    write_run(sandbox.results, now_id(5), {"AAA": sig()}, [buy_thesis("AAA")])
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 100.0}))
    fb.submit_faults["AAA"] = [("accept_then_raise",
                                api_error(422, 40010001, "client_order_id must be unique"))]
    ex.execute()
    assert "AAA" in state_now(sandbox)
    assert len(buys(sandbox, "AAA")) == 1
    assert len(fb.orders_for("AAA", "buy")) == 1, "no second order may be sent"


def test_buy_that_really_failed_is_not_recorded_and_not_resent(sandbox, monkeypatch):
    write_run(sandbox.results, now_id(5), {"AAA": sig()}, [buy_thesis("AAA")])
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 100.0}))
    fb.submit_faults["AAA"] = [("raise", api_error(403, 40310000, "insufficient buying power"))]
    ex.execute()
    assert "AAA" not in state_now(sandbox)
    assert not trades(sandbox)
    assert len(fb.submitted) == 1, "an order call must never be retried by _read"


def test_unknown_buy_outcome_stops_further_buys(sandbox, monkeypatch):
    """F7: accepted but the lookup failed: the book may hold a position we
    cannot see, so nothing else may be sized against it this run."""
    write_run(sandbox.results, now_id(5), {"AAA": sig(0.5), "BBB": sig(0.4)},
              [buy_thesis("AAA"), buy_thesis("BBB")])
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 100.0, "BBB": 100.0}))
    fb.submit_faults["AAA"] = [("accept_then_raise", requests.ReadTimeout("read timed out"))]
    fb.fail["get_order_by_client_id"] = [server_error() for _ in range(3)]
    ex.execute()
    assert [r.symbol for r in fb.submitted] == ["AAA"]
    assert alerts_matching(sandbox, "UNKNOWN")


def test_unknown_buy_is_protected_by_the_next_manage(sandbox, monkeypatch):
    """F7 follow-through: the unseen position gets a stop, not silence."""
    write_run(sandbox.results, now_id(5), {"AAA": sig()}, [buy_thesis("AAA")])
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 100.0}))
    fb.plans[("AAA", "buy")] = Plan("fill", after=0)
    fb.submit_faults["AAA"] = [("accept_then_raise", requests.ReadTimeout("read timed out"))]
    fb.fail["get_order_by_client_id"] = [server_error() for _ in range(3)]
    ex.execute()
    assert "AAA" in fb.book
    ex.manage()
    st = state_now(sandbox)["AAA"]
    assert 0 < st["stop"] < 100.0


def test_unfilled_buy_is_pending_not_a_position(sandbox, monkeypatch):
    """F11 + round 2: an unfilled buy is canceled after FILL_WAIT_S; if the
    cancel is not confirmed either, it is recorded as a pending buy with a
    thesis entry (the order may still fill)."""
    write_run(sandbox.results, now_id(5), {"AAA": sig()}, [buy_thesis("AAA")])
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 100.0}))
    fb.plans[("AAA", "buy")] = Plan("never")
    fb.cancel_modes["AAA"] = "stuck"      # halted venue: the cancel is never confirmed
    ex.execute()
    st = state_now(sandbox)["AAA"]
    oid = fb.orders_for("AAA", "buy")[0].id
    assert fb.cancel_calls == [oid], "the unfilled market buy was not canceled"
    assert st["pending_buy"]["order_id"] == oid and st["entry_source"] == "thesis"
    assert buys(sandbox, "AAA")[0]["fill_status"] != "filled"


def test_pending_buy_is_never_reconciled_as_a_phantom_exit(sandbox, monkeypatch):
    """F11: the broker shows no position because the buy has not filled."""
    write_run(sandbox.results, now_id(5), {"AAA": sig()}, [buy_thesis("AAA")])
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 100.0}))
    fb.plans[("AAA", "buy")] = Plan("never")
    fb.cancel_modes["AAA"] = "stuck"      # halted venue: the cancel is never confirmed
    ex.execute()
    ex.manage()
    assert "AAA" in state_now(sandbox)
    assert not sells(sandbox) and not [t for t in trades(sandbox) if t["action"] == "reconcile_unresolved"]


def test_pending_buy_canceled_later_drops_state_without_an_exit(sandbox, monkeypatch):
    write_run(sandbox.results, now_id(5), {"AAA": sig()}, [buy_thesis("AAA")])
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 100.0}))
    fb.plans[("AAA", "buy")] = Plan("never")
    fb.cancel_modes["AAA"] = "stuck"      # halted venue: the cancel is never confirmed
    ex.execute()
    fb.cancel_order(fb.orders_for("AAA", "buy")[0].id)
    ex.manage()
    assert "AAA" not in state_now(sandbox)
    assert not sells(sandbox)
    assert not [t for t in trades(sandbox) if t["action"] == "reconcile_unresolved"]


def test_pending_buy_filled_later_is_reanchored_to_the_fill(sandbox, monkeypatch):
    write_run(sandbox.results, now_id(5), {"AAA": sig()},
              [buy_thesis("AAA", entry=100.0, stop=99.0, target=101.0)])
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 100.0}))
    fb.plans[("AAA", "buy")] = Plan("never")
    fb.cancel_modes["AAA"] = "stuck"      # halted venue: the cancel is never confirmed
    ex.execute()
    fb.prices["AAA"] = 110.0
    fb.fill_order(fb.orders_for("AAA", "buy")[0].id, price=110.0)
    ex.manage()
    st = state_now(sandbox)["AAA"]
    assert st["entry"] == 110.0 and st["entry_source"] == "fill" and "pending_buy" not in st
    assert st["stop"] <= round(110.0 * 0.92, 2), "the rule floor applies to the real entry"
    assert st["target"] > 110.0, "a target at or under the fill is lifted"


def test_pending_buy_from_an_earlier_run_blocks_a_second_buy(sandbox, monkeypatch):
    """F7/F11: a buy still working at the broker is part of the book. The next
    hour's execute must not buy the same ticker again (a double buy that also
    overwrites the first order's state), nor ignore it in the limits."""
    write_run(sandbox.results, now_id(70), {"AAA": sig()}, [buy_thesis("AAA")])
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 100.0}))
    fb.plans[("AAA", "buy")] = Plan("never")
    fb.cancel_modes["AAA"] = "stuck"      # halted venue: the cancel is never confirmed
    ex.execute()
    first = fb.orders_for("AAA", "buy")[0].id
    write_run(sandbox.results, now_id(5), {"AAA": sig()}, [buy_thesis("AAA")])
    ex.execute()
    assert len(fb.orders_for("AAA", "buy")) == 1, "second buy of a ticker with a pending buy"
    assert state_now(sandbox)["AAA"]["pending_buy"]["order_id"] == first


def test_rerun_of_the_same_scan_does_not_adopt_the_old_order(sandbox, monkeypatch):
    """F12: the same scan executed again an hour later reuses the
    client_order_id; the broker 422s and the lookup returns the OLD order."""
    rid = now_id(5)
    write_run(sandbox.results, rid, {"AAA": sig()}, [buy_thesis("AAA")])
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 100.0}))
    ex.execute()
    old = fb.orders_for("AAA", "buy")[0]
    fb.age_order(old.id, minutes=60)
    # the position was stopped out in between
    del fb.book["AAA"]
    st = state_now(sandbox)
    st.pop("AAA")
    risk_engine.save_positions_state(st)
    ex.execute()
    assert "AAA" not in state_now(sandbox)
    assert len(buys(sandbox, "AAA")) == 1
    assert len(fb.orders_for("AAA", "buy")) == 1


@pytest.mark.parametrize("kind", ["reject", "cancel"])
def test_recovered_dead_order_is_not_a_position(sandbox, monkeypatch, kind):
    """F13: the recovered order's status matters."""
    write_run(sandbox.results, now_id(5), {"AAA": sig()}, [buy_thesis("AAA")])
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 100.0}))
    fb.plans[("AAA", "buy")] = Plan(kind, after=0)
    fb.submit_faults["AAA"] = [("accept_then_raise", api_error(504, 50400000, "gateway timeout"))]
    ex.execute()
    assert "AAA" not in state_now(sandbox)
    assert not buys(sandbox)


@pytest.mark.parametrize("kind", ["reject", "cancel", "expire"])
def test_submit_recovery_returns_nothing_for_a_dead_order(sandbox, kind):
    """F13 at the unit: the recovered order itself must be alive (the fill
    poll downstream would also catch it; this pins the recovery rule)."""
    fb = FakeBroker(prices={"AAA": 100.0})
    fb.plans[("AAA", "buy")] = Plan(kind, after=0)
    fb.submit_faults["AAA"] = [("accept_then_raise", api_error(504, 50400000, "gateway timeout"))]
    got = ex._submit_notional_buy(fb, "AAA", 1000.0, "sov-x-AAA", datetime.now(timezone.utc))
    assert got is None


def test_submit_recovery_returns_this_attempts_live_order(sandbox):
    fb = FakeBroker(prices={"AAA": 100.0})
    fb.plans[("AAA", "buy")] = Plan("never")
    fb.submit_faults["AAA"] = [("accept_then_raise", api_error(504, 50400000, "gateway timeout"))]
    got = ex._submit_notional_buy(fb, "AAA", 1000.0, "sov-x-AAA", datetime.now(timezone.utc))
    assert got is not None and str(got.id) == fb.orders_for("AAA", "buy")[0].id


def test_submit_that_raised_and_never_reached_the_broker_reraises(sandbox):
    fb = FakeBroker(prices={"AAA": 100.0})
    err = api_error(403, 40310000, "insufficient buying power")
    fb.submit_faults["AAA"] = [("raise", err)]
    with pytest.raises(APIError) as e:
        ex._submit_notional_buy(fb, "AAA", 1000.0, "sov-x-AAA", datetime.now(timezone.utc))
    assert e.value is err


def test_buy_rejected_after_acceptance_records_nothing(sandbox, monkeypatch):
    write_run(sandbox.results, now_id(5), {"AAA": sig()}, [buy_thesis("AAA")])
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 100.0}))
    fb.plans[("AAA", "buy")] = Plan("reject", after=2)
    ex.execute()
    assert "AAA" not in state_now(sandbox) and not buys(sandbox)


def test_partially_filled_then_canceled_buy_is_still_a_position(sandbox, monkeypatch):
    """A DAY order that fills half and is then canceled leaves real shares. It
    is not 'nothing happened': it must be recorded (buy logged, stop kept)."""
    write_run(sandbox.results, now_id(5), {"AAA": sig()}, [buy_thesis("AAA")])
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 100.0}))
    fb.plans[("AAA", "buy")] = Plan("partial", after=1, frac=0.5, then="cancel", then_after=1)
    ex.execute()
    assert "AAA" in fb.book
    assert "AAA" in state_now(sandbox), "shares exist at the broker but nothing was recorded"
    assert len(buys(sandbox, "AAA")) == 1


def test_execute_with_a_corrupt_state_file_places_no_order(sandbox, monkeypatch):
    """F10 / docstring 'a corrupt state file stops the run': placing an order
    the run already cannot record is not stopping the run."""
    sandbox.positions.write_text('{"AAA": {"entry": 1')
    write_run(sandbox.results, now_id(5), {"BBB": sig()}, [buy_thesis("BBB")])
    fb = use(monkeypatch, FakeBroker(prices={"BBB": 100.0}))
    try:
        ex.execute()
    except Exception:
        pass
    assert fb.submitted == [], "bought with a state file it could not write"
    assert sandbox.positions.read_text() == '{"AAA": {"entry": 1'


# ==========================================================================
# 6. manage: exits confirmed by fills (F1, F2, F5, F6)
# ==========================================================================

def test_accepted_but_unfilled_exit_keeps_state_as_pending(sandbox, monkeypatch):
    """F1: a sell stuck in a halt. Nothing is logged; the state is kept."""
    track("HALT")
    fb = use(monkeypatch, FakeBroker())
    fb.hold("HALT", 10, 100.0, 85.0)
    fb.plans[("HALT", "sell")] = Plan("never")
    ex.manage()
    st = state_now(sandbox)["HALT"]
    assert st["pending_exit"]["order_id"] == fb.orders_for("HALT", "sell")[0].id
    assert st["stop"] == 92.0
    assert not sells(sandbox)


def test_pending_exit_is_not_readopted_or_resold(sandbox, monkeypatch):
    """F1: next run, still halted: no looser stop, no second close."""
    track("HALT")
    fb = use(monkeypatch, FakeBroker())
    fb.hold("HALT", 10, 100.0, 85.0)
    fb.plans[("HALT", "sell")] = Plan("never")
    ex.manage()
    fb.prices["HALT"] = 70.0
    ex.manage()
    st = state_now(sandbox)["HALT"]
    assert fb.close_calls == ["HALT"]
    assert st["stop"] == 92.0 and st["reason"] == "t", "re-adopted with a new stop"
    assert not sells(sandbox)


def test_pending_exit_logged_once_when_it_fills(sandbox, monkeypatch):
    """F1: the exit is logged exactly once, with the real fill."""
    track("HALT")
    fb = use(monkeypatch, FakeBroker())
    fb.hold("HALT", 10, 100.0, 85.0)
    fb.plans[("HALT", "sell")] = Plan("never")
    ex.manage()
    fb.fill_order(fb.orders_for("HALT", "sell")[0].id, price=84.0)
    ex.manage()
    ex.manage()
    s = sells(sandbox, "HALT")
    assert len(s) == 1 and s[0]["fill_price"] == 84.0 and s[0]["pnl_pct"] == pytest.approx(-0.16)
    assert s[0]["order_id"] == fb.orders_for("HALT", "sell")[0].id
    assert "HALT" not in state_now(sandbox)


def test_partially_filled_exit_stays_pending_until_filled(sandbox, monkeypatch):
    track("PART")
    fb = use(monkeypatch, FakeBroker())
    fb.hold("PART", 10, 100.0, 85.0)
    fb.plans[("PART", "sell")] = Plan("partial", after=1, frac=0.4)
    ex.manage()
    assert fb.book["PART"]["qty"] == pytest.approx(6.0)
    ex.manage()
    assert fb.close_calls == ["PART"] and not sells(sandbox)
    assert state_now(sandbox)["PART"]["pending_exit"]
    fb.fill_order(fb.orders_for("PART", "sell")[0].id, price=85.0)
    ex.manage()
    assert len(sells(sandbox, "PART")) == 1 and "PART" not in state_now(sandbox)


def test_exit_order_that_dies_rearms_the_original_stop(sandbox, monkeypatch):
    track("HALT")
    fb = use(monkeypatch, FakeBroker())
    fb.hold("HALT", 10, 100.0, 85.0)
    fb.plans[("HALT", "sell")] = Plan("never")
    ex.manage()
    fb.cancel_order(fb.orders_for("HALT", "sell")[0].id)
    fb.plans[("HALT", "sell")] = Plan("fill", after=1)
    ex.manage()
    assert alerts_matching(sandbox, "Exit order died")
    assert fb.close_calls == ["HALT", "HALT"], "the stop must be re-armed and re-fired"
    assert len(sells(sandbox, "HALT")) == 1


def test_exit_rejected_at_once_keeps_the_stop_armed(sandbox, monkeypatch):
    track("REJ")
    fb = use(monkeypatch, FakeBroker())
    fb.hold("REJ", 10, 100.0, 85.0)
    fb.plans[("REJ", "sell")] = Plan("reject", after=1)
    ex.manage()
    st = state_now(sandbox)["REJ"]
    assert st["stop"] == 92.0 and "pending_exit" not in st
    assert not sells(sandbox) and alerts_matching(sandbox, "REJECTED")


def test_close_that_raised_but_filled_is_logged(sandbox, monkeypatch):
    """F2 positive case: a 504'd DELETE the broker accepted; the resend got
    403 insufficient qty. The broker's own live sell proves the close."""
    track("GONE", entry=300.00, stop=276.00, target=345)
    fb = use(monkeypatch, FakeBroker())
    fb.hold("GONE", 10, 300.0, 275.0)
    fb.close_faults["GONE"] = [("accept_then_raise", api_error(
        403, 40310000, "insufficient qty available for order", held_for_orders="10"))]
    ex.manage()
    s = sells(sandbox, "GONE")
    assert len(s) == 1 and s[0]["fill_price"] == 275.0 and s[0]["pnl_pct"] < 0
    assert "GONE" not in state_now(sandbox)
    q = fb.order_queries[-1]
    assert q.status == QueryOrderStatus.ALL and q.symbols == ["GONE"], \
        "status must be ALL: the server default (open) hides a filled sell"


@pytest.mark.parametrize("case", ["canceled", "rejected", "expired", "other_symbol", "old"])
def test_close_recovery_rejects_bad_evidence(sandbox, monkeypatch, case):
    """F2: a canceled/rejected/expired sell, another symbol's sell, or an old
    sell is not proof that THIS close went through."""
    track("GONE", entry=300.00, stop=276.00, target=345)
    fb = use(monkeypatch, FakeBroker(prices={"OTHER": 50.0}))
    fb.hold("GONE", 10, 300.0, 275.0)
    if case in ("canceled", "rejected", "expired"):
        fb.seed_order("GONE", "sell", case, minutes_ago=0.2, qty=10)
    elif case == "other_symbol":
        fb.seed_order("OTHER", "sell", "filled", minutes_ago=0.2, price=50.0, qty=10)
    else:
        fb.seed_order("GONE", "sell", "filled", minutes_ago=10, price=280.0, qty=10)
    fb.close_faults["GONE"] = [("raise", api_error(500, 50010000, "internal server error"))]
    ex.manage()
    assert not sells(sandbox)
    assert "GONE" in state_now(sandbox) and "pending_exit" not in state_now(sandbox)["GONE"]
    assert alerts_matching(sandbox, "Exit FAILED")


def test_close_that_really_failed_alerts_and_keeps_state(sandbox, monkeypatch):
    track("GONE", entry=300.00, stop=276.00, target=345)
    fb = use(monkeypatch, FakeBroker())
    fb.hold("GONE", 10, 300.0, 275.0)
    fb.close_faults["GONE"] = [("raise", api_error(403, 40310000, "trading halted for symbol"))]
    ex.manage()
    assert not sells(sandbox)
    assert "GONE" in state_now(sandbox)
    assert alerts_matching(sandbox, "FAILED")
    assert fb.close_calls == ["GONE"], "a close must never be retried by the code"


def test_one_failed_exit_does_not_skip_the_others(sandbox, monkeypatch):
    """F5 / docstring: each exit is isolated."""
    track("AAA")
    track("BBB")
    fb = use(monkeypatch, FakeBroker())
    fb.hold("AAA", 10, 100.0, 85.0)
    fb.hold("BBB", 10, 100.0, 85.0)
    fb.close_faults["AAA"] = [("raise", server_error())]
    ex.manage()
    assert fb.close_calls == ["AAA", "BBB"]
    assert [t["ticker"] for t in sells(sandbox)] == ["BBB"]
    assert "AAA" in state_now(sandbox) and "BBB" not in state_now(sandbox)


def test_log_failure_keeps_state_and_the_exit_is_recovered_once(sandbox, monkeypatch):
    """F5: the trade-log line is written BEFORE the state is deleted. A log
    write that fails leaves the stop state; the next run reconciles the sell
    from the broker and logs it exactly once. The other stop still runs."""
    track("AAA")
    track("BBB")
    fb = use(monkeypatch, FakeBroker())
    fb.hold("AAA", 10, 100.0, 85.0)
    fb.hold("BBB", 10, 100.0, 85.0)
    real = ex._log_trade
    boom = {"left": 1}

    def flaky(rec):
        if rec.get("action") == "sell" and boom["left"]:
            boom["left"] -= 1
            raise OSError("No space left on device")
        return real(rec)
    monkeypatch.setattr(ex, "_log_trade", flaky)
    ex.manage()
    assert "AAA" in state_now(sandbox), "state deleted with no exit record"
    assert [t["ticker"] for t in sells(sandbox)] == ["BBB"]
    ex.manage()
    ex.manage()
    a = sells(sandbox, "AAA")
    assert len(a) == 1 and a[0]["fill_price"] == 85.0
    assert a[0]["order_id"] == fb.orders_for("AAA", "sell")[0].id
    assert "AAA" not in state_now(sandbox)


def test_crash_after_logging_never_double_logs(sandbox, monkeypatch):
    """F6: the order id is the dedupe key; a rerun over the same fill logs nothing new."""
    track("GONE")
    fb = use(monkeypatch, FakeBroker())
    fb.hold("GONE", 10, 100.0, 85.0)
    real = risk_engine.save_positions_state
    boom = {"left": 1}

    def flaky(state):
        if "GONE" not in state and boom["left"]:
            boom["left"] -= 1
            raise OSError("killed")
        return real(state)
    monkeypatch.setattr(ex, "save_positions_state", flaky)
    ex.manage()
    assert len(sells(sandbox, "GONE")) == 1 and "GONE" in state_now(sandbox)
    ex.manage()
    assert len(sells(sandbox, "GONE")) == 1
    assert "GONE" not in state_now(sandbox)


def test_log_exit_refuses_a_duplicate_order_id(sandbox):
    assert ex._log_exit("AAA", "STOP", "oid-1", 90.0, 10.0, {"entry": 100.0}, True, "manage")
    assert not ex._log_exit("AAA", "STOP", "oid-1", 90.0, 10.0, {"entry": 100.0}, True, "reconciled")
    s = sells(sandbox)
    assert len(s) == 1 and s[0]["qty"] == 10.0 and s[0]["partial"] is False
    assert s[0]["pnl_pct"] == pytest.approx(-0.1)


def test_log_exit_dedupes_a_partial_separately(sandbox):
    """Round 2: a partial fill is logged once for the shares it sold; the same
    partial is never logged twice."""
    assert ex._log_exit("AAA", "STOP (partial fill)", "oid-2", 85.0, 4.0, {"entry": 100.0},
                        True, "manage", partial=True)
    assert not ex._log_exit("AAA", "STOP (partial fill)", "oid-2", 85.0, 4.0, {"entry": 100.0},
                            True, "manage", partial=True)
    s = sells(sandbox)
    assert len(s) == 1 and s[0]["qty"] == 4.0 and s[0]["partial"] is True


# ==========================================================================
# 7. manage: positions the broker no longer shows (F3, F8)
# ==========================================================================

def test_one_empty_positions_response_wipes_nothing(sandbox, monkeypatch):
    """F3: a transient empty list is not 'everything was sold'."""
    track("AAA")
    track("BBB")
    fb = use(monkeypatch, FakeBroker())
    fb.hold("AAA", 10, 100.0, 99.0)
    fb.hold("BBB", 10, 100.0, 99.0)
    fb.positions_script = [[]]
    before = state_now(sandbox)
    ex.manage()
    after = state_now(sandbox)
    assert set(after) == {"AAA", "BBB"}
    assert after["AAA"]["stop"] == before["AAA"]["stop"]
    assert not trades(sandbox)


def test_positions_list_empty_but_broker_confirms_held(sandbox, monkeypatch):
    """F3 / docstring: 'gone' is confirmed per symbol (get_open_position -> 404)."""
    track("AAA")
    fb = use(monkeypatch, FakeBroker())
    fb.hold("AAA", 10, 100.0, 99.0)
    fb.positions_script = [[], [], [], []]
    ex.manage()
    assert "AAA" in state_now(sandbox) and not trades(sandbox)


def test_confirm_gone_error_keeps_state(sandbox, monkeypatch):
    track("AAA")
    fb = use(monkeypatch, FakeBroker())
    fb.open_position_errors["AAA"] = server_error()
    ex.manage()
    assert "AAA" in state_now(sandbox) and not trades(sandbox)


def test_vanished_position_with_a_filled_sell_is_reconciled(sandbox, monkeypatch):
    track("GONE", entry=300.00, stop=276.00, target=345)
    fb = use(monkeypatch, FakeBroker(prices={"GONE": 275.9}))
    fb.seed_order("GONE", "sell", "filled", minutes_ago=0, price=275.90, qty=10)
    ex.manage()
    s = sells(sandbox, "GONE")
    assert len(s) == 1 and s[0]["source"] == "reconciled" and s[0]["fill_price"] == 275.90
    assert "GONE" not in state_now(sandbox)
    assert alerts_matching(sandbox, "GONE")


def test_vanished_position_with_a_working_sell_becomes_pending(sandbox, monkeypatch):
    track("GONE")
    fb = use(monkeypatch, FakeBroker(prices={"GONE": 90.0}))
    live = fb.seed_order("GONE", "sell", "accepted", minutes_ago=0, qty=10)
    ex.manage()
    assert state_now(sandbox)["GONE"]["pending_exit"]["order_id"] == live.id
    assert not sells(sandbox)


def test_sell_from_before_the_position_opened_is_not_evidence(sandbox, monkeypatch):
    """F2 in the reconcile path: an old sell of the same symbol."""
    fb = use(monkeypatch, FakeBroker(prices={"GONE": 90.0}))
    fb.seed_order("GONE", "sell", "filled", minutes_ago=60 * 24, price=120.0, qty=10)
    track("GONE")
    ex.manage()
    assert not sells(sandbox)
    assert [t["action"] for t in trades(sandbox)] == ["reconcile_unresolved"]


def test_vanished_with_no_sell_is_a_record_not_a_trade(sandbox, monkeypatch):
    track("GONE")
    use(monkeypatch, FakeBroker())
    ex.manage()
    t = trades(sandbox)
    assert [r["action"] for r in t] == ["reconcile_unresolved"]
    assert "GONE" not in state_now(sandbox)
    assert [a for a in sandbox.alerts if a[2].get("level") == "critical"]


def test_no_sell_reconcile_does_not_reset_the_loss_streak(sandbox, monkeypatch):
    """F8: reconciled exits with no broker sell used to break the streak."""
    append_log(sandbox, {"action": "sell", "why": "STOP hit", "pnl_pct": -0.08, "order_id": "a"},
               {"action": "sell", "why": "STOP hit", "pnl_pct": -0.06, "order_id": "b"})
    assert risk_engine.get_consecutive_losses() == 2
    track("GONE")
    use(monkeypatch, FakeBroker())
    ex.manage()
    assert risk_engine.get_consecutive_losses() == 2


# ==========================================================================
# 8. manage: failures fail safe (F4, F9, F10, _read)
# ==========================================================================

def test_clock_outage_in_market_hours_still_enforces_stops(sandbox, monkeypatch):
    """F4: an unreadable clock is NOT 'market closed'."""
    track("AAA")
    fb = use(monkeypatch, FakeBroker())
    fb.hold("AAA", 10, 100.0, 85.0)
    fb.fail["get_clock"] = [server_error() for _ in range(5)]
    pin_exchange_time(monkeypatch, WEDNESDAY_11AM)
    ex.manage()
    assert fb.close_calls == ["AAA"] and len(sells(sandbox, "AAA")) == 1
    assert alerts_matching(sandbox, "clock unreadable")


def test_clock_outage_on_a_weekend_queues_exits_and_says_so(sandbox, monkeypatch):
    track("AAA")
    fb = use(monkeypatch, FakeBroker())
    fb.hold("AAA", 10, 100.0, 85.0)
    fb.fail["get_clock"] = [server_error() for _ in range(5)]
    pin_exchange_time(monkeypatch, SATURDAY_11AM)
    ex.manage()
    assert fb.close_calls == [] and "AAA" in state_now(sandbox)
    assert alerts_matching(sandbox, "clock unreadable")


def test_market_closed_queues_exits_without_touching_state(sandbox, monkeypatch):
    track("AAA")
    fb = use(monkeypatch, FakeBroker(market_open=False))
    fb.hold("AAA", 10, 100.0, 85.0)
    ex.manage()
    assert fb.close_calls == []
    st = state_now(sandbox)["AAA"]
    assert st["stop"] == 92.0 and "pending_exit" not in st


def test_account_outage_still_enforces_stops(sandbox, monkeypatch):
    """F9: get_account down skips only the circuit breaker and the snapshot."""
    track("AAA")
    fb = use(monkeypatch, FakeBroker())
    fb.hold("AAA", 10, 100.0, 85.0)
    fb.fail["get_account"] = [server_error() for _ in range(5)]
    ex.manage()
    assert fb.close_calls == ["AAA"] and len(sells(sandbox, "AAA")) == 1
    assert not (sandbox.state / "account_snapshot.json").exists()
    assert not risk_engine.HALT_FILE.exists()
    assert alerts_matching(sandbox, "account unreadable")


def test_transient_read_failures_are_retried(sandbox, monkeypatch):
    """F15: _read retries a READ (twice here) and the run carries on."""
    track("AAA")
    fb = use(monkeypatch, FakeBroker())
    fb.hold("AAA", 10, 100.0, 85.0)
    fb.fail["get_all_positions"] = [rate_limited(), server_error()]
    ex.manage()
    assert fb.close_calls == ["AAA"]


def test_read_gives_up_after_its_attempts(sandbox):
    calls = []

    def always():
        calls.append(1)
        raise server_error()
    with pytest.raises(APIError):
        ex._read(always)
    assert len(calls) == 3
    assert sandbox.clock.sleeps == [2.0, 4.0]


def test_read_returns_on_first_success(sandbox):
    seq = [server_error(), "ok"]

    def flaky():
        v = seq.pop(0)
        if isinstance(v, Exception):
            raise v
        return v
    assert ex._read(flaky) == "ok"


@pytest.mark.parametrize("content", ["{", "", "[]", '{"AAA": {"entry": 1', "null"])
def test_corrupt_state_stops_manage_and_is_not_overwritten(sandbox, monkeypatch, content):
    """F10: never read a damaged state file as {} and overwrite it."""
    sandbox.positions.write_text(content)
    fb = use(monkeypatch, FakeBroker())
    fb.hold("AAA", 10, 100.0, 50.0)
    ex.manage()
    assert sandbox.positions.read_text() == content
    assert fb.close_calls == []
    assert alerts_matching(sandbox, "CORRUPT")


def test_missing_state_file_is_an_empty_book(sandbox):
    assert not sandbox.positions.exists()
    assert risk_engine.load_positions_state() == {}


def test_save_json_is_atomic(tmp_path, monkeypatch):
    """F10: a write that dies half-way leaves the previous file intact."""
    p = tmp_path / "positions_state.json"
    sovereign_config.save_json(p, {"AAA": {"stop": 92.0}})

    def die(fd):
        raise OSError("disk full")
    monkeypatch.setattr(sovereign_config.os, "fsync", die)
    with pytest.raises(OSError):
        sovereign_config.save_json(p, {"AAA": {"stop": 1.0}, "BBB": {}})
    assert json.loads(p.read_text()) == {"AAA": {"stop": 92.0}}


def test_snapshot_failure_does_not_block_exits(sandbox, monkeypatch):
    track("GONE", entry=300.00, stop=276.00, target=345)
    fb = use(monkeypatch, FakeBroker())
    fb.hold("GONE", 10, 300.0, 270.0)
    monkeypatch.setattr(ex, "write_account_snapshot", lambda *a, **k: 1 / 0)
    ex.manage()
    assert fb.close_calls == ["GONE"]


SNAPSHOT_KEYS = {"timestamp", "paper", "equity", "cash", "last_equity", "positions"}
SNAPSHOT_POSITION_KEYS = {"symbol", "qty", "avg_entry", "current", "unrealized_pl",
                          "unrealized_plpc", "stop", "target"}


def test_manage_writes_snapshot_with_contract_keys(sandbox, monkeypatch):
    track("HELD", entry=400.00, stop=402.00, target=460.00)
    fb = use(monkeypatch, FakeBroker(market_open=False))
    fb.hold("HELD", 30, 400.0, 450.0)
    ex.manage()
    snap = json.loads((sandbox.state / "account_snapshot.json").read_text())
    assert set(snap) == SNAPSHOT_KEYS
    assert set(snap["positions"][0]) == SNAPSHOT_POSITION_KEYS
    assert snap["positions"][0]["stop"] == 402.00


# ==========================================================================
# 9. evaluate_exits / reconcile_entry (pure; F15)
# ==========================================================================

def _pos(symbol, qty, avg, price):
    fb = FakeBroker()
    fb.hold(symbol, qty, avg, price)
    return fb._position(symbol)


def test_evaluate_exits_saves_nothing_and_deletes_nothing(sandbox):
    state = {"AAA": {"entry": 100.0, "stop": 92.0, "target": 115.0, "trail_armed": False},
             "GONE": {"entry": 50.0, "stop": 46.0, "target": 57.5, "trail_armed": False}}
    actions = risk_engine.evaluate_exits([_pos("AAA", 10, 100.0, 90.0)], state)
    assert [a["ticker"] for a in actions] == ["AAA"] and "STOP" in actions[0]["why"]
    assert "GONE" in state
    assert not sandbox.positions.exists()


def test_evaluate_exits_skips_pending_exit_entries(sandbox):
    """A sell is already working: never re-sold, never re-adopted."""
    state = {"P1": {"entry": 100.0, "stop": 92.0, "target": 115.0, "trail_armed": False,
                    "pending_exit": {"order_id": "x"}}}
    before = json.loads(json.dumps(state))
    assert risk_engine.evaluate_exits([_pos("P1", 10, 100.0, 50.0)], state) == []
    assert state == before


def test_evaluate_exits_enforces_the_stop_on_a_held_pending_buy(sandbox):
    """Round 2 (finding 2): a pending buy whose shares the broker holds IS a
    position; its stop applies to the held shares. It is not re-adopted."""
    state = {"P2": {"entry": 100.0, "stop": 92.0, "target": 115.0, "trail_armed": False,
                    "reason": "t", "pending_buy": {"order_id": "y"}}}
    acts = risk_engine.evaluate_exits([_pos("P2", 10, 90.0, 50.0)], state)
    assert [(a["ticker"], a["action"]) for a in acts] == [("P2", "sell")] and "STOP" in acts[0]["why"]
    assert state["P2"]["reason"] == "t" and state["P2"]["pending_buy"] == {"order_id": "y"}


def test_untracked_position_is_adopted_with_a_default_stop():
    state = {}
    risk_engine.evaluate_exits([_pos("NEW", 5, 40.0, 50.0)], state)
    assert state["NEW"]["stop"] == round(50.0 * 0.92, 2)
    assert state["NEW"]["target"] == round(40.0 * 1.15, 2)


def test_breakeven_raises_once_then_stops_out():
    state = {"AAA": {"entry": 100.0, "stop": 92.0, "target": 130.0, "trail_armed": False}}
    assert risk_engine.evaluate_exits([_pos("AAA", 10, 100.0, 111.0)], state) == []
    assert state["AAA"]["stop"] == 100.5 and state["AAA"]["trail_armed"]
    acts = risk_engine.evaluate_exits([_pos("AAA", 10, 100.0, 100.4)], state)
    assert len(acts) == 1 and "STOP" in acts[0]["why"]


def test_target_hit_sells():
    state = {"AAA": {"entry": 100.0, "stop": 92.0, "target": 115.0, "trail_armed": False}}
    acts = risk_engine.evaluate_exits([_pos("AAA", 10, 100.0, 116.0)], state)
    assert len(acts) == 1 and "TARGET" in acts[0]["why"] and acts[0]["qty"] == "10.0"


def test_reconcile_entry_widens_a_stop_the_real_entry_made_too_tight():
    # filled 5% under the thesis: stop 92 is now only 3.2% under the real entry
    st = {"entry": 100.0, "stop": 92.0, "target": 115.0, "trail_armed": False}
    assert risk_engine.reconcile_entry("AAA", st, 95.0)
    assert st["entry"] == 95.0 and st["thesis_entry"] == 100.0 and st["entry_source"] == "fill"
    assert st["stop"] == round(95.0 * 0.92, 2)
    assert st["target"] == 115.0


def test_reconcile_entry_keeps_a_wider_stop_and_lifts_a_passed_target():
    # filled 10% over the thesis: stop 92 is now WIDER than the rule (kept);
    # target 104 is under the real entry (lifted)
    st = {"entry": 100.0, "stop": 92.0, "target": 104.0, "trail_armed": False}
    assert risk_engine.reconcile_entry("AAA", st, 110.0)
    assert st["entry"] == 110.0 and st["stop"] == 92.0
    assert st["target"] == round(110.0 * 1.15, 2)


@pytest.mark.parametrize("avg,stop", [(104.0, round(104.0 * 1.005, 2)),   # real fill higher: re-armed up
                                       (96.0, 100.5)])                     # lower: never moves down
def test_reconcile_entry_rearms_an_armed_breakeven_on_the_real_fill(avg, stop):
    """a82d324 (round-3 finding 18): an armed breakeven only moves up -- it is
    re-armed on the real fill, stop = max(stop, new_entry * 1.005). It used to
    be left alone, i.e. below the real fill."""
    st = {"entry": 100.0, "stop": 100.5, "target": 130.0, "trail_armed": True}
    assert risk_engine.reconcile_entry("AAA", st, avg)
    assert st["entry"] == avg and st["stop"] == stop and st["trail_armed"] is True
    assert st["target"] == 130.0
    assert "_stop_lowered" not in st


def test_reconcile_entry_ignores_noise_and_zero():
    st = {"entry": 100.0, "stop": 92.0, "target": 115.0, "trail_armed": False}
    assert not risk_engine.reconcile_entry("AAA", st, 100.05)
    assert not risk_engine.reconcile_entry("AAA", st, 0.0)
    assert st["entry"] == 100.0 and "thesis_entry" not in st


def test_manage_reanchors_entry_to_the_broker_average(sandbox, monkeypatch):
    track("FILL", entry=200.00, stop=174.00, target=230)
    fb = use(monkeypatch, FakeBroker())
    fb.hold("FILL", 17.5, 204.0, 199.0)
    ex.manage()
    st = state_now(sandbox)["FILL"]
    assert st["entry"] == 204.00 and st["thesis_entry"] == 200.00


# ==========================================================================
# 10. loss streak (kept + F8, F15 boundary)
# ==========================================================================

def test_breakeven_stop_is_not_a_loss(sandbox):
    append_log(sandbox, {"action": "sell", "why": "STOP hit", "pnl_pct": -0.06},
               {"action": "sell", "why": "STOP hit: 401.90 <= 402.00", "pnl_pct": 0.004})
    assert risk_engine.get_consecutive_losses() == 0


def test_losses_counted_by_pnl(sandbox):
    append_log(sandbox, {"action": "sell", "why": "TARGET hit", "pnl_pct": 0.15},
               {"action": "sell", "why": "reconciled", "pnl_pct": -0.08},
               {"action": "sell", "why": "STOP hit", "pnl_pct": -0.05})
    assert risk_engine.get_consecutive_losses() == 2


def test_legacy_records_still_use_the_stop_label(sandbox):
    append_log(sandbox, {"action": "sell", "why": "STOP hit"}, {"action": "sell", "why": "STOP hit"})
    assert risk_engine.get_consecutive_losses() == 2


def test_zero_pnl_exit_breaks_the_streak(sandbox):
    """F15 boundary: pnl == 0 is not a loss."""
    append_log(sandbox, {"action": "sell", "why": "STOP hit", "pnl_pct": -0.05},
               {"action": "sell", "why": "STOP hit", "pnl_pct": -0.05},
               {"action": "sell", "why": "STOP hit", "pnl_pct": 0.0})
    assert risk_engine.get_consecutive_losses() == 0


def test_priceless_reconciled_record_neither_counts_nor_breaks(sandbox):
    """F8: a reconciled record with no price says nothing about win or loss."""
    append_log(sandbox, {"action": "sell", "why": "STOP", "pnl_pct": -0.05, "order_id": "a"},
               {"action": "sell", "why": "STOP", "pnl_pct": -0.05, "order_id": "b"},
               {"action": "sell", "source": "reconciled", "why": "reconciled", "pnl_pct": None,
                "order_id": "c"})
    assert risk_engine.get_consecutive_losses() == 2


def test_an_exit_logged_twice_counts_once(sandbox):
    append_log(sandbox, {"action": "sell", "why": "TARGET", "pnl_pct": 0.1, "order_id": "w"},
               {"action": "sell", "why": "STOP", "pnl_pct": -0.05, "order_id": "a"},
               {"action": "sell", "why": "STOP", "pnl_pct": -0.05, "order_id": "a"})
    assert risk_engine.get_consecutive_losses() == 1


# ==========================================================================
# 11. live_price: trade, else tight mid, else last close; never the ask
# ==========================================================================

class FakeData:
    """StockHistoricalDataClient's latest-trade/quote surface, returning real
    alpaca-py Trade/Quote models (Trade.timestamp is a required, tz-aware
    datetime). `trade_ts` defaults to one minute before the pinned exchange
    time, i.e. a fresh regular-session print."""
    def __init__(self, trade=None, bid=None, ask=None, trade_raises=None, quote_raises=None,
                 trade_ts=None):
        self.trade, self.bid, self.ask = trade, bid, ask
        self.trade_raises, self.quote_raises = trade_raises, quote_raises
        self.trade_ts = trade_ts
        self.requests = []

    def get_stock_latest_trade(self, req):
        from alpaca.data.models import Trade
        self.requests.append(req)
        if self.trade_raises:
            raise self.trade_raises
        sym = req.symbol_or_symbols[0]
        if self.trade is None:
            return {}
        ts = self.trade_ts or (FrozenET.pinned or datetime.now(ET)) - timedelta(minutes=1)
        return {sym: Trade(sym, {"t": ts.astimezone(timezone.utc).isoformat(),
                                 "p": float(self.trade), "s": 100})}

    def get_stock_latest_quote(self, req):
        from alpaca.data.models import Quote
        self.requests.append(req)
        if self.quote_raises:
            raise self.quote_raises
        sym = req.symbol_or_symbols[0]
        if self.bid is None:
            return {}
        return {sym: Quote(sym, {"t": datetime.now(timezone.utc).isoformat(), "bp": float(self.bid),
                                 "ap": float(self.ask), "bs": 1, "as": 1})}


@pytest.fixture
def exchange_at(monkeypatch):
    """Pin sovereign_pipeline's idea of 'now in New York' (live_price's trade
    freshness is judged against it). Default: Wednesday 11:00 ET."""
    import sovereign_pipeline as sp

    def pin(when=WEDNESDAY_11AM):
        FrozenET.pinned = when
        monkeypatch.setattr(sp, "datetime", FrozenET)
        return when
    pin()
    return pin


def _lp(data, fallback=97.0):
    import sovereign_pipeline as sp
    return sp.live_price(data, "AAA", fallback, StockLatestTradeRequest, StockLatestQuoteRequest)


def test_live_price_prefers_the_latest_trade(exchange_at):
    d = FakeData(trade=100.5, bid=99.0, ask=110.0)
    assert _lp(d) == 100.5
    assert isinstance(d.requests[0], StockLatestTradeRequest)
    assert d.requests[0].symbol_or_symbols == ["AAA"]


def test_live_price_uses_the_mid_under_one_percent_spread(exchange_at):
    assert _lp(FakeData(trade_raises=server_error(), bid=100.0, ask=100.5)) == pytest.approx(100.25)
    assert _lp(FakeData(trade=None, bid=100.0, ask=100.5)) == pytest.approx(100.25)
    assert _lp(FakeData(trade=0.0, bid=100.0, ask=100.5)) == pytest.approx(100.25)


def test_live_price_wide_spread_falls_back_to_the_close_never_the_ask(exchange_at):
    got = _lp(FakeData(trade=None, bid=100.0, ask=108.0), fallback=97.0)
    assert got == 97.0 and got not in (108.0, 104.0)


def test_live_price_exactly_one_percent_is_not_tight(exchange_at):
    assert _lp(FakeData(trade=None, bid=99.5, ask=100.5), fallback=97.0) == 97.0


def test_live_price_with_no_bid_never_uses_the_ask(exchange_at):
    assert _lp(FakeData(trade=None, bid=0.0, ask=101.0), fallback=97.0) == 97.0


def test_live_price_everything_down_returns_the_fallback(exchange_at):
    assert _lp(FakeData(trade_raises=server_error(), quote_raises=server_error()),
               fallback=97.0) == 97.0


def test_get_stock_data_prices_from_the_trade_in_market_hours(monkeypatch, exchange_at):
    import alpaca.data.historical as hist
    import sovereign_pipeline as sp
    d = FakeData(trade=123.45, bid=100.0, ask=130.0)
    seen = []

    def bars(req):
        seen.append(req)
        raise RuntimeError("offline")
    d.get_stock_bars = bars
    monkeypatch.setattr(hist, "StockHistoricalDataClient", lambda *a, **k: d)
    data = sp.get_stock_data("AAA", market_open=True)
    assert data.current_price == 123.45
    assert seen and seen[0].adjustment == Adjustment.ALL


# ==========================================================================
# 12. split/dividend-adjusted bars at every StockBarsRequest site
# ==========================================================================

BAR_SITES = [
    ("sovereign_pipeline", lambda m: m.get_stock_data("AAA")),
    ("risk_engine", lambda m: m._return_series(["AAA", "BBB"])),
    ("bt_data", lambda m: m._fetch_alpaca(["AAA"], datetime(2026, 1, 1), datetime(2026, 2, 1))),
    ("correlation_breaks", lambda m: m._daily_closes(["AAA", "BBB"])),
    ("macro_regime", lambda m: m._score_sector_dispersion()),
    ("macro_regime", lambda m: m._score_trend()),
    ("member_scoring", lambda m: m._fetch_bars(["AAA"], datetime(2026, 1, 1))),
    ("sovereign_backtest", lambda m: m._bars(["AAA"], datetime(2026, 1, 1))),
    ("sovereign_opportunity", lambda m: m.scan_movers()),
]


@pytest.mark.parametrize("module,call", BAR_SITES,
                         ids=[f"{m}-{i}" for i, (m, _) in enumerate(BAR_SITES)])
def test_bar_requests_are_split_adjusted(monkeypatch, module, call):
    import importlib
    import alpaca.data.historical as hist
    seen = []

    class Client:
        def __init__(self, *a, **k):
            pass

        def get_stock_bars(self, req):
            seen.append(req)
            raise RuntimeError("offline")

        def __getattr__(self, name):
            def refuse(*a, **k):
                raise RuntimeError("offline")
            return refuse
    monkeypatch.setattr(hist, "StockHistoricalDataClient", Client)
    mod = importlib.import_module(module)
    try:
        call(mod)
    except Exception:
        pass
    assert seen, f"{module}: no bar request reached the client"
    assert all(r.adjustment == Adjustment.ALL for r in seen), [r.adjustment for r in seen]


def _adjustment_all_checker(src: str):
    """(sites, bad) for every StockBarsRequest(...) call in `src`, bare-name
    or attribute-style (`requests.StockBarsRequest(...)`). A site is good only
    if it passes adjustment=Adjustment.ALL (through any alias of alpaca's
    Adjustment enum) or a module name bound to exactly that (`_ADJ_ALL`)."""
    tree = ast.parse(src)
    aliases, modules = set(), set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module in ("alpaca.data.enums", "alpaca.data"):
            aliases |= {a.asname or a.name for a in n.names if a.name == "Adjustment"}
        if isinstance(n, ast.Import):
            modules |= {a.asname or a.name for a in n.names if a.name == "alpaca.data.enums"}

    def is_all(v):
        if not (isinstance(v, ast.Attribute) and v.attr == "ALL"):
            return False
        base = v.value
        if isinstance(base, ast.Name):
            return base.id in aliases
        return (isinstance(base, ast.Attribute) and base.attr == "Adjustment"
                and ast.unparse(base.value) in modules)
    bound = {t.id for n in ast.walk(tree) if isinstance(n, ast.Assign) and is_all(n.value)
             for t in n.targets if isinstance(t, ast.Name)}
    sites, bad = [], []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
        if name != "StockBarsRequest":
            continue
        sites.append(node.lineno)
        v = {k.arg: k.value for k in node.keywords}.get("adjustment")
        if not (v is not None and (is_all(v) or (isinstance(v, ast.Name) and v.id in bound))):
            bad.append(node.lineno)
    return sites, bad


def test_every_bar_request_in_the_codebase_is_adjustment_all():
    """Structural (round 2, finding 19): naming `adjustment=` is not enough --
    it must be Adjustment.ALL (or _ADJ_ALL bound to it), and attribute-style
    calls count. A raw request brings back a 4:1 split read as a 75% crash."""
    sites, missing = [], []
    for path in sorted(REPO.glob("*.py")):
        s, bad = _adjustment_all_checker(path.read_text())
        sites += [(path.name, ln) for ln in s]
        missing += [(path.name, ln) for ln in bad]
    assert len(sites) >= 9, sites
    assert not missing, missing


@pytest.mark.parametrize("src,ok", [
    ("from alpaca.data.requests import StockBarsRequest\nStockBarsRequest(symbol_or_symbols=['A'])", False),
    ("from alpaca.data.enums import Adjustment\nStockBarsRequest(adjustment=Adjustment.RAW)", False),
    ("from alpaca.data.enums import Adjustment\nStockBarsRequest(adjustment=Adjustment.SPLIT)", False),
    ("import alpaca.data.requests as r\nr.StockBarsRequest(symbol_or_symbols=['A'])", False),
    ("from alpaca.data.enums import Adjustment\nclass Adjustment2: ALL = 1\n"
     "StockBarsRequest(adjustment=Adjustment2.ALL)", False),
    ("from alpaca.data.enums import Adjustment as A\n_ADJ_ALL = A.RAW\nStockBarsRequest(adjustment=_ADJ_ALL)", False),
    ("StockBarsRequest(**kw)", False),
    ("from alpaca.data.enums import Adjustment\nStockBarsRequest(adjustment=Adjustment.ALL)", True),
    ("from alpaca.data.enums import Adjustment as _A\n_ADJ_ALL = _A.ALL\n"
     "import alpaca.data.requests as r\nr.StockBarsRequest(adjustment=_ADJ_ALL)", True),
    ("import alpaca.data.enums\nStockBarsRequest(adjustment=alpaca.data.enums.Adjustment.ALL)", True),
])
def test_the_adjustment_checker_can_fail(src, ok):
    """The structural check above is only worth something if it goes red."""
    sites, bad = _adjustment_all_checker(src)
    assert len(sites) == 1 and (not bad) == ok, (sites, bad)


def test_every_module_level_adj_all_is_adjustment_all():
    import importlib
    for path in sorted(REPO.glob("*.py")):
        if "_ADJ_ALL =" in path.read_text():
            mod = importlib.import_module(path.stem)
            assert mod._ADJ_ALL is Adjustment.ALL, path.name


# ==========================================================================
# 13. congress decay clocked from the newest filing
# ==========================================================================

def _herd(days_ago, direction="buy"):
    from congress_scraper import HerdSignal
    lf = "" if days_ago is None else (date.today() - timedelta(days=days_ago)).isoformat()
    return HerdSignal(ticker="XYZ", direction=direction,
                      members=[{"name": "Jane Doe"}, {"name": "John Roe"}], latest_filing=lf)


@pytest.mark.parametrize("days_ago,weight", [(0, 1.0), (20, 1.0), (30, 1.0), (45, 0.5),
                                             (59, 1 / 30), (60, 0.0), (90, 0.0)])
def test_congress_decay_weights_by_filing_age(days_ago, weight):
    import signal_aggregator as sa
    c = sa._congress_score("XYZ", [_herd(days_ago)], {})
    assert c.score == pytest.approx(round(0.4 * weight, 3), abs=1e-3)   # 2 members -> 0.4


def test_congress_decay_applies_to_sells_too():
    import signal_aggregator as sa
    assert sa._congress_score("XYZ", [_herd(45, "sell")], {}).score == pytest.approx(-0.2)


def test_unknown_filing_date_means_no_decay_and_says_so():
    import signal_aggregator as sa
    c = sa._congress_score("XYZ", [_herd(None)], {})
    assert c.score == pytest.approx(0.4)
    assert "filing date unknown" in c.detail


def test_herd_recency_days_reads_objects_and_dicts():
    import signal_aggregator as sa
    today = date(2026, 9, 25)
    assert sa.herd_recency_days({"latest_filing": "2026-08-11"}, today=today) == 45
    assert sa.herd_recency_days(SimpleNamespace(latest_filing="2026-09-25"), today=today) == 0
    assert sa.herd_recency_days({"latest_filing": ""}, today=today) == 0
    assert sa.herd_recency_days({"latest_filing": "garbage"}, today=today) == 0
    assert sa.herd_recency_days({}, today=today) == 0


def test_detect_herds_stamps_the_newest_filing():
    from congress_scraper import Transaction, detect_herds

    def d(n):
        return (datetime.now() - timedelta(days=n)).strftime("%m/%d/%Y")
    txs = [Transaction(member="Jane Doe", chamber="house", state="CA", ticker="XYZ", asset_name="x",
                       direction="buy", amount_range="$1K", transaction_date=d(40), filing_date=d(12)),
           Transaction(member="John Roe", chamber="house", state="NY", ticker="XYZ", asset_name="x",
                       direction="buy", amount_range="$1K", transaction_date=d(38), filing_date=d(5))]
    herds = detect_herds(txs)
    assert len(herds) == 1
    assert herds[0].latest_filing == (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")


def test_detect_herds_without_parseable_filings_leaves_it_unknown():
    from congress_scraper import Transaction, detect_herds

    def d(n):
        return (datetime.now() - timedelta(days=n)).strftime("%m/%d/%Y")
    txs = [Transaction(member=m, chamber="house", state="CA", ticker="XYZ", asset_name="x",
                       direction="buy", amount_range="$1K", transaction_date=d(10), filing_date="")
           for m in ("Jane Doe", "John Roe")]
    assert detect_herds(txs)[0].latest_filing == ""


# ==========================================================================
# 14. round 2 (R2-1 .. R2-19): the findings 24f9eb4 claims to fix, tested
#     against its commit message and the module docstring. Includes ports of
#     the round-2 reviewers' scratch demonstrations and the four GAP probes.
# ==========================================================================

def enospc(*a, **k):
    raise OSError(errno.ENOSPC, "No space left on device")


def hours_ago(h):
    return (datetime.now() - timedelta(hours=h)).isoformat()


def any_alert_mentions(sb, text):
    return [a for a in sb.alerts if text.lower() in (a[0] + " " + " ".join(map(str, a[1]))).lower()]


def raw_log_records(sb):
    """Trade-log records, tolerating unparseable (torn) lines."""
    out = []
    for line in (sb.state / "trade_log.jsonl").read_text().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            out.append({"_torn": line})
    return out


# ---- fake faithfulness for the new broker surface ------------------------

def test_fake_cancel_is_async_and_refuses_a_closed_order():
    fb = FakeBroker(prices={"AAA": 10.0})
    fb.plans["AAA"] = Plan("never")
    o = fb.submit_order(MarketOrderRequest(symbol="AAA", notional=10.0, client_order_id="c",
                                           side=OrderSide.BUY, time_in_force=TimeInForce.DAY))
    assert fb.cancel_order_by_id(o.id) is None
    assert fb.orders[str(o.id)].status == "pending_cancel"
    assert fb.get_order_by_id(o.id).status == OrderStatus.CANCELED
    with pytest.raises(APIError) as e:
        fb.cancel_order_by_id(o.id)
    assert e.value.status_code == 422
    with pytest.raises(APIError) as e:
        fb.cancel_order_by_id(uuid.uuid4())
    assert e.value.status_code == 404


def test_fake_replaced_order_carries_replaced_by():
    fb = FakeBroker(prices={"AAA": 10.0})
    old = fb.seed_order("AAA", "sell", "accepted", qty=5)
    new = fb.replace_order(old.id)
    got = fb.get_order_by_id(old.id)
    assert got.status == OrderStatus.REPLACED and str(got.replaced_by) == new.id


@pytest.mark.parametrize("status", ["stopped", "suspended", "done_for_day", "held"])
def test_fake_open_query_includes_non_terminal_statuses(status):
    """Alpaca's status=open returns every order that is not in a final state."""
    fb = FakeBroker(prices={"AAA": 10.0})
    o = fb.seed_order("AAA", "buy", status)
    assert [str(x.id) for x in fb.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))] == [o.id]


def test_fake_position_can_carry_null_prices():
    fb = FakeBroker()
    fb.hold("AAA", 10, 100.0, 90.0)
    fb.null_price.add("AAA")
    p = fb.get_all_positions()[0]
    assert isinstance(p, Position) and p.current_price is None and p.market_value is None


# ---- R2-1: execute ignored tracked state ---------------------------------

def test_r2_1_execute_does_not_buy_over_a_pending_exit_whose_fill_is_then_logged(sandbox, monkeypatch):
    """Port (order-safety N1 / failure-modes F2): manage's stop-out sell had
    not filled by its wait; it filled before execute. The scan still likes the
    ticker. No buy, the pending exit survives, and manage logs the fill once."""
    fb = use(monkeypatch, FakeBroker(prices={"XYZ": 100.0}))
    sell = fb.seed_order("XYZ", "sell", "filled", minutes_ago=20, price=85.0, qty=10)
    track("XYZ", pending_exit={"order_id": sell.id, "why": "STOP hit", "since": hours_ago(0.3)})
    write_run(sandbox.results, now_id(5), {"XYZ": sig()}, [buy_thesis("XYZ")])
    ex.execute()
    assert fb.submitted == [], "bought a ticker whose stop-out is still pending"
    assert state_now(sandbox)["XYZ"]["pending_exit"]["order_id"] == sell.id
    ex.manage()
    s = sells(sandbox, "XYZ")
    assert [t["order_id"] for t in s] == [sell.id] and s[0]["fill_price"] == 85.0
    assert "XYZ" not in state_now(sandbox)


def test_r2_1_record_entry_refuses_to_overwrite_a_tracked_entry(sandbox):
    track("XYZ", pending_exit={"order_id": "o1", "why": "STOP", "since": hours_ago(0)})
    before = state_now(sandbox)
    with pytest.raises(ValueError):
        track("XYZ", entry=50.0)
    assert state_now(sandbox) == before


def test_r2_1_tracked_pending_buy_counts_in_the_book(sandbox, monkeypatch):
    """Whole book: a pending buy in state (no broker position yet) uses up
    sector room. NVDA and AMD share a sector; the cap is 20% of 100k."""
    monkeypatch.setitem(RISK, "sector_cap_pct", 0.20)
    risk_engine.record_entry("NVDA", 100.0, 15000.0, 92.0, 115.0, reason="t",
                             pending_buy={"order_id": None, "client_order_id": "sov-x-NVDA",
                                          "since": hours_ago(1)})
    write_run(sandbox.results, now_id(5), {"AMD": sig(0.4)}, [buy_thesis("AMD")])
    fb = use(monkeypatch, FakeBroker(prices={"AMD": 100.0}))
    ex.execute()
    assert [(r.symbol, r.notional) for r in fb.submitted] == [("AMD", 5000.0)]


def test_r2_1_open_buy_order_at_the_broker_counts_in_the_book(sandbox, monkeypatch):
    """An open buy order the state does not know about (lost state, manual
    order) blocks its ticker and uses up sector room."""
    monkeypatch.setitem(RISK, "sector_cap_pct", 0.20)
    write_run(sandbox.results, now_id(5), {"NVDA": sig(0.5), "AMD": sig(0.4)},
              [buy_thesis("NVDA"), buy_thesis("AMD")])
    fb = use(monkeypatch, FakeBroker(prices={"NVDA": 100.0, "AMD": 100.0}))
    fb.seed_order("NVDA", "buy", "accepted", minutes_ago=30, notional=15000.0)
    ex.execute()
    assert [(r.symbol, r.notional) for r in fb.submitted] == [("AMD", 5000.0)]


def test_r2_1_open_buy_order_counts_toward_max_positions(sandbox, monkeypatch):
    monkeypatch.setitem(RISK, "max_positions", 1)
    write_run(sandbox.results, now_id(5), {"AAA": sig()}, [buy_thesis("AAA")])
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 100.0, "ZZZ": 50.0}))
    fb.seed_order("ZZZ", "buy", "new", minutes_ago=30, notional=1000.0)
    ex.execute()
    assert fb.submitted == []


# ---- R2-2: partial fills --------------------------------------------------

def test_r2_2_partial_buy_is_a_position_at_its_actual_fill(sandbox, monkeypatch):
    write_run(sandbox.results, now_id(5), {"AAA": sig()}, [buy_thesis("AAA")])
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 100.0}))
    fb.plans[("AAA", "buy")] = Plan("partial", after=1, frac=0.5, price=98.0, then="cancel", then_after=1)
    ex.execute()
    rec = fb.orders_for("AAA", "buy")[0]
    assert rec.status == "canceled" and rec.filled_qty > 0
    st = state_now(sandbox)["AAA"]
    assert "pending_buy" not in st and st["entry"] == 98.0 and st["entry_source"] == "fill"
    assert st["notional"] == pytest.approx(round(98.0 * rec.filled_qty, 2))
    assert st["stop"] == round(98.0 * 0.92, 2)
    b = buys(sandbox, "AAA")
    assert len(b) == 1 and b[0]["fill_status"] == "partial" and b[0]["fill_price"] == 98.0
    assert b[0]["qty"] == pytest.approx(rec.filled_qty)


def test_r2_2_unfilled_buy_is_canceled_after_fill_wait_and_nothing_recorded(sandbox, monkeypatch):
    """A market buy unfilled after FILL_WAIT_S (a halt) is canceled; with
    nothing filled there is no position, no state and no buy line."""
    write_run(sandbox.results, now_id(5), {"AAA": sig()}, [buy_thesis("AAA")])
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 100.0}))
    fb.plans[("AAA", "buy")] = Plan("never")
    ex.execute()
    rec = fb.orders_for("AAA", "buy")[0]
    assert fb.cancel_calls == [rec.id] and rec.status == "canceled"
    assert sum(sandbox.clock.sleeps) >= ex.FILL_WAIT_S, "canceled before FILL_WAIT_S elapsed"
    assert "AAA" not in state_now(sandbox) and not buys(sandbox)


def test_r2_2_partially_filled_buy_is_canceled_after_the_wait_and_the_fill_kept(sandbox, monkeypatch):
    write_run(sandbox.results, now_id(5), {"AAA": sig()}, [buy_thesis("AAA")])
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 100.0}))
    fb.plans[("AAA", "buy")] = Plan("partial", after=1, frac=0.5, then=None)
    ex.execute()
    rec = fb.orders_for("AAA", "buy")[0]
    assert fb.cancel_calls == [rec.id] and rec.status == "canceled"
    st = state_now(sandbox)["AAA"]
    assert "pending_buy" not in st and st["entry"] == 100.0
    assert st["notional"] == pytest.approx(7500.0, abs=0.01)
    assert fb.book["AAA"]["qty"] == pytest.approx(75.0)


def test_r2_2_buy_that_fills_as_it_is_canceled_is_a_position(sandbox, monkeypatch):
    write_run(sandbox.results, now_id(5), {"AAA": sig()}, [buy_thesis("AAA")])
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 100.0}))
    fb.plans[("AAA", "buy")] = Plan("never")
    fb.cancel_modes["AAA"] = "fills_first"
    ex.execute()
    st = state_now(sandbox)["AAA"]
    assert "pending_buy" not in st and st["entry"] == 100.0
    assert buys(sandbox, "AAA")[0]["fill_status"] == "filled"


def test_r2_2_buy_whose_cancel_fails_is_pending_not_forgotten(sandbox, monkeypatch):
    write_run(sandbox.results, now_id(5), {"AAA": sig()}, [buy_thesis("AAA")])
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 100.0}))
    fb.plans[("AAA", "buy")] = Plan("never")
    fb.cancel_modes["AAA"] = "raise"
    ex.execute()
    rec = fb.orders_for("AAA", "buy")[0]
    assert state_now(sandbox)["AAA"]["pending_buy"]["order_id"] == rec.id
    assert len(buys(sandbox, "AAA")) == 1


def test_r2_2_partial_exit_is_logged_for_the_shares_sold_and_the_rest_is_stopped(sandbox, monkeypatch):
    """Port (test-quality BUG partial exit): a close fills 4 of 10 and dies.
    The 4 are logged (qty 4, partial), the stop stays armed on 6, and the
    next run sells the 6."""
    track("PART")
    fb = use(monkeypatch, FakeBroker())
    fb.hold("PART", 10, 100.0, 85.0)
    fb.plans[("PART", "sell")] = Plan("partial", after=1, frac=0.4, then="cancel", then_after=1)
    ex.manage()
    first = fb.orders_for("PART", "sell")[0]
    s = sells(sandbox, "PART")
    assert len(s) == 1 and s[0]["order_id"] == first.id and s[0]["partial"] is True
    assert s[0]["qty"] == pytest.approx(4.0) and s[0]["fill_price"] == 85.0
    st = state_now(sandbox)["PART"]
    assert "pending_exit" not in st and st["stop"] == 92.0
    assert alerts_matching(sandbox, "Partial exit")
    fb.plans[("PART", "sell")] = Plan("fill", after=1)
    ex.manage()
    s = sells(sandbox, "PART")
    assert [t["qty"] for t in s] == pytest.approx([4.0, 6.0])
    assert "PART" not in state_now(sandbox) and "PART" not in fb.book


def test_r2_2_pending_exit_partially_filled_then_expired_is_logged(sandbox, monkeypatch):
    """Port (order-safety N2): a DAY close sold 6 of 10 and expired."""
    fb = use(monkeypatch, FakeBroker())
    fb.hold("PRT", 4, 100.0, 85.0)
    sell = fb.seed_order("PRT", "sell", "expired", minutes_ago=60, qty=10, filled=6, price=85.0)
    track("PRT", pending_exit={"order_id": sell.id, "why": "STOP hit", "since": hours_ago(1)})
    fb.plans[("PRT", "sell")] = Plan("fill", after=1)
    ex.manage()
    s = sells(sandbox, "PRT")
    assert s[0]["order_id"] == sell.id and s[0]["partial"] is True and s[0]["qty"] == 6.0
    assert s[0]["pnl_pct"] == pytest.approx(-0.15)
    assert fb.close_calls == ["PRT"], "the re-armed stop did not fire on the 4 shares left"
    assert len(s) == 2 and s[1]["qty"] == pytest.approx(4.0)


def test_r2_2_pending_buy_canceled_with_a_partial_fill_keeps_its_own_stop(sandbox, monkeypatch):
    """Port (order-safety): 3 of 10 filled then canceled. Not 'dead', not
    re-adopted with a fresh stop under today's price."""
    fb = use(monkeypatch, FakeBroker())
    fb.hold("PBY", 3, 100.0, 105.0)
    buy = fb.seed_order("PBY", "buy", "canceled", minutes_ago=60, qty=10, filled=3, price=100.0)
    track("PBY", pending_buy={"order_id": buy.id, "client_order_id": buy.client_order_id,
                              "since": hours_ago(1)}, order_id=buy.id)
    ex.manage()
    st = state_now(sandbox)["PBY"]
    assert st["reason"] == "t" and "pending_buy" not in st
    assert st["entry"] == 100.0 and st["notional"] == 300.0 and st["stop"] == 92.0


def test_r2_2_pending_buy_canceled_with_a_partial_fill_below_its_stop_is_sold(sandbox, monkeypatch):
    fb = use(monkeypatch, FakeBroker())
    fb.hold("PBD", 3, 100.0, 85.0)
    buy = fb.seed_order("PBD", "buy", "canceled", minutes_ago=60, qty=10, filled=3, price=100.0)
    track("PBD", pending_buy={"order_id": buy.id, "client_order_id": buy.client_order_id,
                              "since": hours_ago(1)}, order_id=buy.id)
    ex.manage()
    assert fb.close_calls == ["PBD"], "thesis stop 92 breached at 85 but re-based, not sold"


def test_r2_2_held_shares_of_a_working_buy_get_their_stop_and_the_buy_is_canceled_first(sandbox, monkeypatch):
    """Port (test-quality BUG / order-safety N2): partially_filled and still
    working. The held shares' stop is enforced; the working buy is canceled
    before the close so it cannot add shares or hold them."""
    write_run(sandbox.results, now_id(5), {"AAA": sig()}, [buy_thesis("AAA")])
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 100.0}))
    fb.plans[("AAA", "buy")] = Plan("partial", after=1, frac=0.5, then=None)
    fb.cancel_modes["AAA"] = "raise"          # the cancel after the wait fails: still working
    ex.execute()
    buy = fb.orders_for("AAA", "buy")[0]
    assert buy.status == "partially_filled" and state_now(sandbox)["AAA"].get("pending_buy")
    fb.cancel_modes["AAA"] = "ok"
    fb.prices["AAA"] = 80.0
    fb.calls, fb.cancel_calls = [], []        # only manage's calls from here
    ex.manage()
    assert fb.close_calls == ["AAA"], "held shares 13% under the stop were not protected"
    assert fb.cancel_calls == [buy.id], "the working buy was not canceled before the exit"
    assert fb.calls.index("cancel_order_by_id") < fb.calls.index("close_position")
    s = sells(sandbox, "AAA")
    assert len(s) == 1 and s[0]["qty"] == pytest.approx(75.0)


def test_r2_2_held_shares_of_a_working_partial_buy_are_stopped_from_their_actual_fill(sandbox, monkeypatch):
    """Docstring: 'a buy that filled partway is a position at its actual
    fill', and a stop is never closer than the 8% rule. 75 shares filled at
    95 against a 100/92 thesis; the buy is still working. At 91 the shares are
    4.2% under their fill -- inside the rule -- so the thesis's 92 (3.2% under
    the real entry) must not fire."""
    write_run(sandbox.results, now_id(5), {"AAA": sig()}, [buy_thesis("AAA", entry=100.0, stop=92.0)])
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 100.0}))
    fb.plans[("AAA", "buy")] = Plan("partial", after=1, frac=0.5, price=95.0, then=None)
    fb.cancel_modes["AAA"] = "raise"
    ex.execute()
    assert state_now(sandbox)["AAA"].get("pending_buy") and fb.book["AAA"]["avg"] == 95.0
    fb.prices["AAA"] = 91.0
    ex.manage()
    assert fb.close_calls == [], "a stop 3.2% under the actual fill (tighter than the rule) fired"


# ---- R2-3: only canceled/expired/rejected are terminal --------------------

@pytest.mark.parametrize("status", ["stopped", "suspended", "done_for_day", "held", "new", "accepted",
                                    "pending_new", "pending_cancel", "pending_replace", "calculated"])
def test_r2_3_non_terminal_statuses_are_open(status):
    fb = FakeBroker(prices={"AAA": 10.0})
    o = fb.seed_order("AAA", "sell", status, qty=10)
    assert ex._order_outcome(fb, o.id, timeout=0).state == "open"


@pytest.mark.parametrize("status", ["canceled", "expired", "rejected"])
def test_r2_3_terminal_statuses_are_dead_with_nothing_filled(status):
    fb = FakeBroker(prices={"AAA": 10.0})
    o = fb.seed_order("AAA", "sell", status, qty=10, filled=0)
    assert ex._order_outcome(fb, o.id, timeout=0) == ex.Outcome("dead", None, 0.0, None)


@pytest.mark.parametrize("status", ["canceled", "expired"])
def test_r2_3_terminal_statuses_with_a_fill_are_partial(status):
    fb = FakeBroker(prices={"AAA": 10.0})
    o = fb.seed_order("AAA", "sell", status, qty=10, filled=4, price=9.5)
    assert ex._order_outcome(fb, o.id, timeout=0) == ex.Outcome("partial", 9.5, 4.0, None)


@pytest.mark.parametrize("status", ["stopped", "suspended", "done_for_day", "held"])
def test_r2_3_pending_exit_in_a_non_terminal_status_is_kept(sandbox, monkeypatch, status):
    """Port (failure-modes F7): 'stopped' guarantees a fill; none of these
    says the order died, so the stop is not re-armed and nothing re-fires."""
    fb = use(monkeypatch, FakeBroker())
    fb.hold("S", 10, 100.0, 85.0)
    sell = fb.seed_order("S", "sell", status, minutes_ago=5, qty=10)
    track("S", pending_exit={"order_id": sell.id, "why": "STOP", "since": hours_ago(0.1)})
    ex.manage()
    assert state_now(sandbox)["S"]["pending_exit"]["order_id"] == sell.id
    assert fb.close_calls == [] and not sells(sandbox)
    assert not alerts_matching(sandbox, "Exit order died")


@pytest.mark.parametrize("status", ["stopped", "suspended", "done_for_day", "held"])
def test_r2_3_buy_in_a_non_terminal_status_is_recorded_as_pending(sandbox, monkeypatch, status):
    """Port (order-safety N3 / test-quality BUG stopped buy): a 'stopped' buy
    is guaranteed to fill; recording nothing drops it from the book."""
    write_run(sandbox.results, now_id(5), {"AAA": sig()}, [buy_thesis("AAA", stop=85.0)])
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 100.0}))
    fb.plans[("AAA", "buy")] = Plan("status", after=1, status=status)
    fb.cancel_modes["AAA"] = "refuse"
    ex.execute()
    rec = fb.orders_for("AAA", "buy")[0]
    assert state_now(sandbox)["AAA"]["pending_buy"]["order_id"] == rec.id
    assert len(buys(sandbox, "AAA")) == 1


# ---- R2-4: the lock -------------------------------------------------------

def _hold_lock(minutes_ago, cmd="execute", holder=True):
    fh = open(ex.LOCK_FILE, "w")
    if holder:
        fh.write(json.dumps({"pid": 4242, "cmd": cmd,
                             "since": (datetime.now() - timedelta(minutes=minutes_ago)).isoformat()}))
        fh.flush()
    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fh


def test_r2_4_the_lock_records_its_holder(sandbox):
    with ex._run_lock("execute") as got:
        assert got is True
        h = json.loads(ex.LOCK_FILE.read_text())
        assert h["pid"] == os.getpid() and h["cmd"] == "execute"
        assert abs((datetime.now() - datetime.fromisoformat(h["since"])).total_seconds()) < 60
        with ex._run_lock("manage") as got2:
            assert got2 is False
    with ex._run_lock("manage") as got3:
        assert got3 is True and json.loads(ex.LOCK_FILE.read_text())["cmd"] == "manage"


def test_r2_4_a_failed_holder_note_cannot_raise_after_the_stops(sandbox, monkeypatch, caplog):
    """Re-review minor (2026-09-25). On a full disk the note's buffered write
    failed at flush and was caught, but the bytes stayed in the buffer, so
    close() retried them and raised AFTER the body (the stops) had run. This
    simulates ENOSPC on the two paths the old and new code take: a buffered
    write through the lock file object fails at flush (and again at close if
    anything is still pending), and a raw write to the lock's descriptor fails.
    Truncating still works, as it does on a full disk. The logged warning
    proves the failure branch ran; without it, a note written through some
    other handle would pass here while its write failed unseen (failure-modes
    reviews, 2026-09-26)."""
    real_open, real_write, lock_fds = open, os.write, set()

    class FullDisk:
        def __init__(self, f):
            self._f, self._pending = f, False
            lock_fds.add(f.fileno())
        def fileno(self): return self._f.fileno()
        def seek(self, *a): return self._f.seek(*a)
        def truncate(self, *a): return self._f.truncate(*a)
        def write(self, s):
            self._pending = True
            return len(s)
        def flush(self):
            if self._pending:
                raise OSError(errno.ENOSPC, "No space left on device")
        def close(self):
            try:
                self.flush()      # a buffered file flushes what is pending at close
            finally:
                self._f.close()
        def __enter__(self): return self
        def __exit__(self, *exc): self.close()

    def fake_open(path, *a, **kw):
        f = real_open(path, *a, **kw)
        return FullDisk(f) if pathlib.Path(path) == pathlib.Path(ex.LOCK_FILE) else f

    def full_write(fd, data):
        if fd in lock_fds:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_write(fd, data)

    monkeypatch.setattr(ex, "open", fake_open, raising=False)
    monkeypatch.setattr(os, "write", full_write)
    ran = []
    with ex._run_lock("manage") as got:     # must raise neither on entry nor on exit
        assert got is True
        ran.append("stops")
    assert ran == ["stops"] and lock_fds, "the lock file never went through the full disk"
    assert "Could not write the lock-holder note" in caplog.text, "the ENOSPC branch never ran"
    with real_open(ex.LOCK_FILE, "a+") as fh:   # released: a fresh descriptor can take it
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fh, fcntl.LOCK_UN)


def test_r2_4_an_error_from_the_body_passes_through_the_lock(sandbox, caplog):
    """Failure-modes review (2026-09-26). With the yield inside the courtesy
    try, an OSError from the body (the stops) was logged as a lock-note failure
    and replaced by "generator didn't stop after throw()": diagnosis pointed
    at the note on a failing disk. The body's error must pass through, and the
    lock must be released."""
    with pytest.raises(OSError, match="from the body"):
        with ex._run_lock("manage") as got:
            assert got is True
            raise OSError(errno.EIO, "from the body")
    assert "Could not write the lock-holder note" not in caplog.text
    with open(ex.LOCK_FILE, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fh, fcntl.LOCK_UN)


def test_r2_4_the_lock_creates_its_state_dir(sandbox, monkeypatch, tmp_path):
    """Failure-modes review (2026-09-26). sovereign_state/ is gitignored, so a
    fresh clone has no state directory and the lock must create it; without
    the mkdir, opening the lock file raises before the stops."""
    fresh = tmp_path / "fresh_state"
    monkeypatch.setattr(ex, "STATE_DIR", fresh)
    monkeypatch.setattr(ex, "LOCK_FILE", fresh / ".sovereign.lock")
    with ex._run_lock("manage") as got:
        assert got is True
    assert (fresh / ".sovereign.lock").exists()


def _lock_fd_tracker(monkeypatch):
    """Route the module's open() through a recorder of the lock file's fds."""
    real_open, fds = open, set()

    def tracking_open(path, *a, **kw):
        f = real_open(path, *a, **kw)
        if pathlib.Path(path) == pathlib.Path(ex.LOCK_FILE):
            fds.add(f.fileno())
        return f
    monkeypatch.setattr(ex, "open", tracking_open, raising=False)
    return fds


def test_r2_4_a_failed_truncate_of_the_holder_note_cannot_stop_the_run(sandbox, monkeypatch, caplog):
    """Failure-modes review (2026-09-26). On a failing disk ftruncate can raise
    EIO. The note is a courtesy, so the body (the stops) must still run: moved
    above the courtesy try, a failed truncate raised before the stops and
    manage exited with no stops sent and no alert. Truncation fails through the
    file object as well as os.ftruncate, so a stray fh.truncate() above the
    courtesy try cannot slip past (final failure-modes review)."""
    real_open, fds, real = open, set(), os.ftruncate

    class EioTruncate:
        def __init__(self, f):
            self._f = f
            fds.add(f.fileno())
        def truncate(self, *a):
            raise OSError(errno.EIO, "Input/output error")
        def __getattr__(self, name):
            return getattr(self._f, name)
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            self._f.close()

    def tracking_open(path, *a, **kw):
        f = real_open(path, *a, **kw)
        return EioTruncate(f) if pathlib.Path(path) == pathlib.Path(ex.LOCK_FILE) else f
    monkeypatch.setattr(ex, "open", tracking_open, raising=False)

    def eio(fd, length):
        if fd in fds:
            raise OSError(errno.EIO, "Input/output error")
        return real(fd, length)
    monkeypatch.setattr(os, "ftruncate", eio)
    ran = []
    with ex._run_lock("manage") as got:
        assert got is True
        ran.append("stops")
    assert ran == ["stops"] and fds
    assert "Could not write the lock-holder note" in caplog.text


def test_r2_4_a_short_holder_note_is_logged(sandbox, monkeypatch, caplog):
    """A short write leaves a truncated note that readers treat as 'holder
    unknown'. It must say so in the log rather than pass silently."""
    fds, real = _lock_fd_tracker(monkeypatch), os.write

    def short(fd, data):
        return real(fd, data[:10]) if fd in fds else real(fd, data)
    monkeypatch.setattr(os, "write", short)
    with ex._run_lock("manage") as got:
        assert got is True
    assert "written short" in caplog.text


@pytest.mark.parametrize("minutes,alerts", [(11, True), (10, True), (9.5, False), (2, False)])
def test_r2_4_manage_blocked_for_ten_minutes_alerts(sandbox, monkeypatch, minutes, alerts):
    """Port (order-safety N4 / failure-modes F4): a hung run holding the lock
    silently disabled every stop behind it."""
    track("LCK")
    fb = use(monkeypatch, FakeBroker())
    fb.hold("LCK", 10, 100.0, 80.0)
    fh = _hold_lock(minutes)
    try:
        ex.manage()
    finally:
        fh.close()
    assert fb.close_calls == [] and fb.calls == []
    got = alerts_matching(sandbox, "lock held")
    assert bool(got) == alerts
    if alerts:
        assert got[0][2]["level"] == "critical"
        text = " ".join(got[0][1])
        assert "4242" in text and "execute" in text


def test_r2_4_manage_blocked_by_an_unknown_holder_alerts(sandbox, monkeypatch):
    fb = use(monkeypatch, FakeBroker())
    fh = _hold_lock(0, holder=False)
    try:
        ex.manage()
    finally:
        fh.close()
    assert alerts_matching(sandbox, "lock held") and fb.calls == []


def test_r2_4_blocked_execute_never_alerts(sandbox, monkeypatch):
    fb = use(monkeypatch, FakeBroker())
    fh = _hold_lock(120, cmd="manage")
    try:
        ex.execute()
    finally:
        fh.close()
    assert fb.calls == [] and not sandbox.alerts


def test_r2_4_correlation_data_client_inside_the_lock_has_a_timeout(monkeypatch):
    """execute -> size_position -> avg_correlation_with_book builds a market
    data client while holding the lock; it must not be able to hang forever."""
    seen = []

    def recorder(self, method, url, **kw):
        seen.append(kw.get("timeout"))
        raise requests.ConnectionError("blocked in tests")
    monkeypatch.setattr(requests.Session, "request", recorder)
    assert risk_engine._return_series(["AAA", "BBB"]) == {}
    assert seen and all(t and t <= 60 for t in seen), seen


def test_r2_4_pipeline_data_client_has_a_timeout(monkeypatch):
    import sovereign_pipeline as sp
    seen = []

    def recorder(self, method, url, **kw):
        seen.append(kw.get("timeout"))
        raise requests.ConnectionError("blocked in tests")
    monkeypatch.setattr(requests.Session, "request", recorder)
    sp.get_stock_data("AAA", market_open=True)
    assert len(seen) >= 2 and all(t and t <= 60 for t in seen), seen


def test_r2_4_cancel_carries_the_trading_timeout(monkeypatch):
    seen = []

    def recorder(self, method, url, **kw):
        seen.append((method, kw.get("timeout")))
        raise requests.ConnectionError("blocked in tests")
    monkeypatch.setattr(requests.Session, "request", recorder)
    client, _ = ex._trading_client()
    with pytest.raises(requests.ConnectionError):
        client.cancel_order_by_id(uuid.uuid4())
    assert seen == [("DELETE", ex.HTTP_TIMEOUT_S)]


# ---- R2-5: staleness bound and replaced orders ----------------------------

@pytest.mark.parametrize("hours,stale", [(19, True), (17, False)])
def test_r2_5_pending_buy_older_than_18h_alerts(sandbox, monkeypatch, hours, stale):
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 100.0}))
    buy = fb.seed_order("AAA", "buy", "accepted", minutes_ago=hours * 60, notional=1000.0)
    track("AAA", pending_buy={"order_id": buy.id, "client_order_id": buy.client_order_id,
                              "since": hours_ago(hours)})
    ex.manage()
    got = alerts_matching(sandbox, "Pending buy STALE")
    assert bool(got) == stale
    if stale:
        assert got[0][2]["level"] == "critical"
    assert state_now(sandbox)["AAA"]["pending_buy"]["order_id"] == buy.id


def test_r2_5_unresolvable_unknown_buy_older_than_18h_alerts(sandbox, monkeypatch):
    """An UnknownOutcome buy whose client_order_id lookup keeps failing (not a
    404) is still a pending buy: 'pending buys/exits have an 18h staleness
    alert'."""
    fb = use(monkeypatch, FakeBroker())
    track("AAA", pending_buy={"order_id": None, "client_order_id": "sov-x-AAA",
                              "since": hours_ago(19)})
    fb.fail["get_order_by_client_id"] = [server_error() for _ in range(3)]
    ex.manage()
    assert "AAA" in state_now(sandbox)
    assert alerts_matching(sandbox, "Pending buy STALE"), "a day-old unresolvable buy raised no alert"


@pytest.mark.parametrize("hours,stale", [(19, True), (17, False)])
def test_r2_5_pending_exit_older_than_18h_alerts(sandbox, monkeypatch, hours, stale):
    fb = use(monkeypatch, FakeBroker())
    fb.hold("HALT", 10, 100.0, 85.0)
    sell = fb.seed_order("HALT", "sell", "accepted", minutes_ago=hours * 60, qty=10)
    track("HALT", pending_exit={"order_id": sell.id, "why": "STOP hit", "since": hours_ago(hours)})
    ex.manage()
    assert bool(alerts_matching(sandbox, "Pending exit STALE")) == stale
    assert state_now(sandbox)["HALT"]["pending_exit"]["order_id"] == sell.id
    assert fb.close_calls == []


@pytest.mark.parametrize("err", [server_error(), api_error(503, 50300000, "service unavailable"),
                                 requests.ConnectionError("connection reset")],
                         ids=["500", "503", "transport"])
@pytest.mark.parametrize("hours,cleared", [(19, True), (17, False)])
def test_r2_5_unreadable_pending_exit_older_than_18h_is_cleared_and_the_stop_fires(
        sandbox, monkeypatch, hours, cleared, err):
    """a82d324: the 18h rule now applies only to an order that is UNREADABLE
    (5xx / transport error). A 404 is 'missing' and re-arms at once (below)."""
    fb = use(monkeypatch, FakeBroker())
    fb.hold("HALT", 10, 100.0, 85.0)
    ghost = str(uuid.uuid4())
    fb.order_errors[ghost] = err              # every read of this order fails, never a 404
    track("HALT", pending_exit={"order_id": ghost, "why": "STOP hit", "since": hours_ago(hours)})
    ex.manage()
    if cleared:
        assert alerts_matching(sandbox, "Pending exit STALE")
        assert fb.close_calls == ["HALT"] and len(sells(sandbox, "HALT")) == 1
        assert "HALT" not in state_now(sandbox)
    else:
        assert fb.close_calls == [] and state_now(sandbox)["HALT"]["pending_exit"]["order_id"] == ghost
        assert not alerts_matching(sandbox, "Pending exit STALE")


def test_r2_5_replaced_exit_order_is_followed_to_its_replacement(sandbox, monkeypatch):
    track("RPL")
    fb = use(monkeypatch, FakeBroker())
    fb.hold("RPL", 10, 100.0, 85.0)
    fb.plans[("RPL", "sell")] = Plan("never")
    ex.manage()
    first = fb.orders_for("RPL", "sell")[0]
    new = fb.replace_order(first.id)
    ex.manage()
    assert state_now(sandbox)["RPL"]["pending_exit"]["order_id"] == new.id
    assert fb.close_calls == ["RPL"], "a second close was sent while the replacement works"
    fb.fill_order(new.id, price=84.0)
    ex.manage()
    s = sells(sandbox, "RPL")
    assert [t["order_id"] for t in s] == [new.id] and s[0]["fill_price"] == 84.0
    assert "RPL" not in state_now(sandbox)


def test_r2_5_replaced_exit_without_a_replacement_does_not_freeze_silently(sandbox, monkeypatch):
    """Port (order-safety N5): a replaced pending exit, stale, price far under
    the stop: three runs with no sell must at least alert."""
    fb = use(monkeypatch, FakeBroker())
    fb.hold("RPL", 10, 100.0, 70.0)
    sell = fb.seed_order("RPL", "sell", "replaced", minutes_ago=60 * 24, qty=10)
    track("RPL", pending_exit={"order_id": sell.id, "why": "STOP hit", "since": hours_ago(24)})
    for _ in range(3):
        ex.manage()
    assert fb.close_calls or alerts_matching(sandbox, "Pending exit STALE")


def test_r2_5_replaced_pending_buy_is_followed_to_its_replacement(sandbox, monkeypatch):
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 100.0}))
    buy = fb.seed_order("AAA", "buy", "accepted", minutes_ago=30, notional=1000.0)
    track("AAA", pending_buy={"order_id": buy.id, "client_order_id": buy.client_order_id,
                              "since": hours_ago(0.5)})
    new = fb.replace_order(buy.id)
    ex.manage()
    assert state_now(sandbox)["AAA"]["pending_buy"]["order_id"] == new.id
    fb.fill_order(new.id, price=101.0)
    ex.manage()
    st = state_now(sandbox)["AAA"]
    assert "pending_buy" not in st and st["entry"] == 101.0 and st["entry_source"] == "fill"


# ---- R2-6 / R2-7: unknown buy outcomes ------------------------------------

def test_r2_6_unknown_buy_is_recorded_as_pending_by_client_order_id(sandbox, monkeypatch):
    write_run(sandbox.results, now_id(5), {"AAA": sig(0.5), "BBB": sig(0.4)},
              [buy_thesis("AAA"), buy_thesis("BBB")])
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 100.0, "BBB": 100.0}))
    fb.plans[("AAA", "buy")] = Plan("never")
    fb.submit_faults["AAA"] = [("accept_then_raise", requests.ReadTimeout("read timed out"))]
    fb.fail["get_order_by_client_id"] = [server_error() for _ in range(3)]
    ex.execute()
    coid = fb.submitted[0].client_order_id
    st = state_now(sandbox)["AAA"]
    assert st["pending_buy"]["client_order_id"] == coid and not st["pending_buy"].get("order_id")
    assert st["entry_source"] == "thesis"
    b = buys(sandbox, "AAA")
    assert len(b) == 1 and b[0]["client_order_id"] == coid and b[0]["fill_status"] == "unknown"
    assert [r.symbol for r in fb.submitted] == ["AAA"] and "BBB" not in state_now(sandbox)
    assert alerts_matching(sandbox, "UNKNOWN")


def test_r2_6_unknown_buy_is_not_bought_again_next_scan(sandbox, monkeypatch):
    """Port (test-quality BUG unknown outcome): here the order never reached
    the broker, so only the state entry can stop the second buy."""
    write_run(sandbox.results, now_id(70), {"AAA": sig()}, [buy_thesis("AAA")])
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 100.0}))
    fb.submit_faults["AAA"] = [("raise", requests.ReadTimeout("read timed out"))]
    ex.execute()
    assert fb.orders_for("AAA", "buy") == [] and "AAA" in state_now(sandbox)
    write_run(sandbox.results, now_id(5), {"AAA": sig()}, [buy_thesis("AAA")])
    ex.execute()
    assert len(fb.submitted) == 1, "bought again while the first buy's outcome is unknown"


def test_r2_6_manage_resolves_an_unknown_buy_by_client_order_id(sandbox, monkeypatch):
    write_run(sandbox.results, now_id(5), {"AAA": sig()}, [buy_thesis("AAA")])
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 101.0}))
    fb.plans[("AAA", "buy")] = Plan("fill", after=0)
    fb.submit_faults["AAA"] = [("accept_then_raise", requests.ReadTimeout("read timed out"))]
    fb.fail["get_order_by_client_id"] = [server_error() for _ in range(3)]
    ex.execute()
    assert state_now(sandbox)["AAA"]["pending_buy"]
    ex.manage()
    st = state_now(sandbox)["AAA"]
    assert "pending_buy" not in st and st["entry"] == 101.0 and st["entry_source"] == "fill"
    assert st["reason"] == "t", "resolved buy was re-adopted instead"


def test_r2_6_unknown_buy_the_broker_never_saw_is_dropped_by_manage(sandbox, monkeypatch):
    write_run(sandbox.results, now_id(5), {"AAA": sig()}, [buy_thesis("AAA")])
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 100.0}))
    fb.submit_faults["AAA"] = [("raise", requests.ReadTimeout("read timed out"))]
    ex.execute()
    assert "AAA" in state_now(sandbox)
    ex.manage()
    assert "AAA" not in state_now(sandbox)
    assert not sells(sandbox)
    assert not [t for t in trades(sandbox) if t["action"] == "reconcile_unresolved"]


@pytest.mark.parametrize("err", [requests.ReadTimeout("read timed out"),
                                 requests.ConnectionError("connection reset"),
                                 api_error(504, 50400000, "gateway timeout"),
                                 api_error(500, 50010000, "internal server error"),
                                 api_error(429, 42910000, "rate limit exceeded")],
                         ids=["read-timeout", "connection", "504", "500", "429"])
def test_r2_7_transient_submit_failure_then_lookup_404_is_unknown(sandbox, err):
    """A 404 right after a timeout/5xx can mean 'still processing'."""
    fb = FakeBroker(prices={"AAA": 100.0})
    fb.submit_faults["AAA"] = [("raise", err)]
    with pytest.raises(ex.UnknownOutcome):
        ex._submit_notional_buy(fb, "AAA", 1000.0, "sov-x-AAA", datetime.now(timezone.utc))


def test_r2_7_order_that_appears_after_the_404_is_a_position_with_its_thesis_stop(sandbox, monkeypatch):
    write_run(sandbox.results, now_id(5), {"AAA": sig()}, [buy_thesis("AAA", stop=85.0)])
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 100.0}))
    fb.submit_faults["AAA"] = [("raise", requests.ReadTimeout("read timed out"))]
    ex.execute()
    req = fb.submitted[0]
    # the broker was still processing the timed-out POST; it lands now
    fb._new_order("AAA", "buy", notional=req.notional, client_order_id=req.client_order_id,
                  plan=Plan("fill", after=0))
    ex.manage()
    st = state_now(sandbox)["AAA"]
    assert st["reason"] == "t" and "pending_buy" not in st
    assert st["entry"] == 100.0 and st["stop"] == 85.0


# ---- R2-8: failures fail safe ---------------------------------------------

def test_r2_8_state_save_failure_after_evaluate_still_sends_the_exit(sandbox, monkeypatch):
    """Port (test-quality BUG state save): the first save (after
    evaluate_exits) fails once; the exit still goes out and it is alerted."""
    track("AAA")
    fb = use(monkeypatch, FakeBroker())
    fb.hold("AAA", 10, 100.0, 85.0)
    real, calls = risk_engine.save_positions_state, {"n": 0}

    def flaky(state):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(state)
    monkeypatch.setattr(ex, "save_positions_state", flaky)
    ex.manage()
    assert fb.close_calls == ["AAA"] and len(sells(sandbox, "AAA")) == 1
    assert alerts_matching(sandbox, "state save FAILED")


def test_r2_8_disk_full_for_every_state_save_still_sends_the_exit(sandbox, monkeypatch):
    """Port (failure-modes F1)."""
    track("STOP")
    fb = use(monkeypatch, FakeBroker())
    fb.hold("STOP", 10, 100.0, 85.0)
    monkeypatch.setattr(ex, "save_positions_state", enospc)
    ex.manage()
    assert fb.close_calls == ["STOP"] and len(sells(sandbox, "STOP")) == 1


def test_r2_8_disk_full_while_the_breaker_trips_still_sends_the_exit(sandbox, monkeypatch):
    """Port (failure-modes F1b): halt.json cannot be written."""
    track("STOP")
    fb = use(monkeypatch, FakeBroker(equity=94000.0, last_equity=100000.0))
    fb.hold("STOP", 10, 100.0, 85.0)
    monkeypatch.setattr(risk_engine, "save_json", enospc)
    ex.manage()
    assert fb.close_calls == ["STOP"]


def test_r2_8_disk_full_with_the_real_alert_still_sends_the_exit(sandbox, monkeypatch):
    """Finding 8 names alert failures too. On a full disk the real alert()
    also fails: its per-day dedupe writes alerts_sent.json. A state-save
    failure's own alert must not abort manage before the exits."""
    import sovereign_alerts
    track("STOP")
    fb = use(monkeypatch, FakeBroker())
    fb.hold("STOP", 10, 100.0, 85.0)
    monkeypatch.setattr(sovereign_alerts, "SENT_FILE", sandbox.state / "alerts_sent.json")
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("SOVEREIGN_SMTP_HOST", raising=False)
    monkeypatch.setattr(ex, "alert", sovereign_alerts.alert)
    for mod in (sovereign_alerts, risk_engine, ex):
        monkeypatch.setattr(mod, "save_json", enospc)
    try:
        ex.manage()
    except OSError as e:
        pytest.fail(f"manage raised {e!r} before any exit; close_calls={fb.close_calls}")
    assert fb.close_calls == ["STOP"]


def test_r2_8_breaker_failure_is_alerted_and_the_exits_still_go_out(sandbox, monkeypatch):
    """Commit 24f9eb4: 'a breaker/snapshot failure are alerted and the exits
    still go out'."""
    track("AAA")
    fb = use(monkeypatch, FakeBroker())
    fb.hold("AAA", 10, 100.0, 85.0)

    def boom(account):
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(ex, "check_circuit_breaker", boom)
    ex.manage()
    assert fb.close_calls == ["AAA"]
    assert any_alert_mentions(sandbox, "breaker"), [a[0] for a in sandbox.alerts]


def test_r2_8_snapshot_failure_is_alerted(sandbox, monkeypatch):
    track("AAA")
    fb = use(monkeypatch, FakeBroker())
    fb.hold("AAA", 10, 100.0, 85.0)
    monkeypatch.setattr(ex, "write_account_snapshot", lambda *a, **k: 1 / 0)
    ex.manage()
    assert fb.close_calls == ["AAA"]
    assert any_alert_mentions(sandbox, "snapshot"), [a[0] for a in sandbox.alerts]


# ---- R2-9: a torn trade-log line ------------------------------------------

def test_r2_9_torn_log_line_does_not_swallow_the_next_exit(sandbox, monkeypatch):
    """Port (failure-modes F5): a previous append died mid-line."""
    torn = '{"action": "buy", "ticker": "AAA", "or'
    (sandbox.state / "trade_log.jsonl").write_text(torn)
    track("STOP")
    fb = use(monkeypatch, FakeBroker())
    fb.hold("STOP", 10, 100.0, 85.0)
    ex.manage()
    recs = raw_log_records(sandbox)
    assert recs[0] == {"_torn": torn}
    assert [r.get("ticker") for r in recs[1:] if r.get("action") == "sell"] == ["STOP"]
    assert risk_engine.get_consecutive_losses() == 1


def test_r2_9_log_trade_repairs_a_torn_tail_every_time(sandbox):
    p = sandbox.state / "trade_log.jsonl"
    p.write_text('{"a": 1}\n{"torn": ')
    ex._log_trade({"action": "x", "n": 1})
    ex._log_trade({"action": "x", "n": 2})
    lines = p.read_text().splitlines()
    assert lines[1] == '{"torn": '
    assert [json.loads(l)["n"] for l in lines[2:]] == [1, 2]
    assert p.read_text().endswith("\n")


def test_r2_9_log_trade_adds_no_blank_line_to_a_healthy_log(sandbox):
    p = sandbox.state / "trade_log.jsonl"
    ex._log_trade({"action": "x", "n": 1})
    ex._log_trade({"action": "x", "n": 2})
    assert p.read_text().count("\n") == 2 and "\n\n" not in p.read_text()


# ---- R2-10: execute reads state before any order --------------------------

def test_r2_10_corrupt_state_alerts_and_places_no_order(sandbox, monkeypatch):
    """Port (failure-modes F6): the state is read before any order."""
    sandbox.positions.write_text('{"AAA": {"entry": 1')
    write_run(sandbox.results, now_id(5), {"BBB": sig()}, [buy_thesis("BBB")])
    fb = use(monkeypatch, FakeBroker(prices={"BBB": 100.0}))
    ex.execute()
    assert fb.submitted == [] and "submit_order" not in fb.calls
    assert alerts_matching(sandbox, "CORRUPT")
    assert sandbox.positions.read_text() == '{"AAA": {"entry": 1'


def test_r2_10_state_write_failure_after_a_buy_leaves_the_buy_line_and_alerts(sandbox, monkeypatch):
    """Port (failure-modes F6b): buy line first, then state; a state failure
    is alerted with the order id."""
    write_run(sandbox.results, now_id(5), {"BBB": sig()}, [buy_thesis("BBB")])
    fb = use(monkeypatch, FakeBroker(prices={"BBB": 100.0}))
    monkeypatch.setattr(risk_engine, "save_json", enospc)
    ex.execute()
    oid = fb.orders_for("BBB", "buy")[0].id
    b = buys(sandbox, "BBB")
    assert len(b) == 1 and b[0]["order_id"] == oid
    a = alerts_matching(sandbox, "Buy NOT recorded")
    assert a and oid in " ".join(a[0][1])


# ---- R2-11: positions outage / empty lists --------------------------------

def test_r2_11_positions_outage_alerts(sandbox, monkeypatch):
    """Port (failure-modes F8)."""
    track("S")
    fb = use(monkeypatch, FakeBroker())
    fb.hold("S", 10, 100.0, 85.0)
    fb.fail["get_all_positions"] = [server_error() for _ in range(3)]
    ex.manage()
    a = alerts_matching(sandbox, "positions unreadable")
    assert a and a[0][2]["level"] == "critical"
    assert fb.close_calls == [] and "S" in state_now(sandbox)


def test_r2_11_two_empty_lists_do_not_skip_a_stop_the_broker_confirms(sandbox, monkeypatch):
    """Port (test-quality BUG two empty lists)."""
    track("AAA")
    fb = use(monkeypatch, FakeBroker())
    fb.hold("AAA", 10, 100.0, 85.0)
    fb.positions_script = [[], []]
    ex.manage()
    assert fb.close_calls == ["AAA"], "stop skipped although get_open_position returned the position"


# ---- R2-12: a null current_price ------------------------------------------

def test_r2_12_null_current_price_does_not_abort_the_other_stops(sandbox, monkeypatch):
    """Port (test-quality BUG null price)."""
    track("AAA")
    track("BBB")
    fb = use(monkeypatch, FakeBroker())
    fb.hold("AAA", 10, 100.0, 100.0)
    fb.hold("BBB", 10, 100.0, 85.0)
    fb.null_price.add("AAA")
    ex.manage()
    assert fb.close_calls == ["BBB"]
    a = alerts_matching(sandbox, "stops NOT evaluated")
    assert a and "AAA" in " ".join(a[0][1]) and a[0][2]["level"] == "critical"
    assert state_now(sandbox)["AAA"]["stop"] == 92.0
    snap = json.loads((sandbox.state / "account_snapshot.json").read_text())
    assert {p["symbol"]: p["current"] for p in snap["positions"]}["AAA"] is None


def test_r2_12_evaluate_exits_reports_a_null_price_as_unevaluated():
    fb = FakeBroker()
    fb.hold("AAA", 10, 100.0, 100.0)
    fb.hold("BBB", 10, 100.0, 85.0)
    fb.null_price.add("AAA")
    state = {s: {"entry": 100.0, "stop": 92.0, "target": 115.0, "trail_armed": False}
             for s in ("AAA", "BBB")}
    acts = risk_engine.evaluate_exits(fb.get_all_positions(), state)
    assert [(a["ticker"], a["action"]) for a in acts] == [("AAA", "unevaluated"), ("BBB", "sell")]


def test_r2_12_null_price_on_an_untracked_position_is_not_adopted_blind():
    fb = FakeBroker()
    fb.hold("NEW", 10, 100.0, 100.0)
    fb.null_price.add("NEW")
    state = {}
    acts = risk_engine.evaluate_exits(fb.get_all_positions(), state)
    assert [a["action"] for a in acts] == ["unevaluated"] and state == {}


# ---- R2-13: live_price freshness ------------------------------------------

def test_r2_13_trade_from_yesterdays_post_market_is_not_the_live_price(exchange_at):
    """Port (evalfixes F-A): IEX at the open still shows yesterday's 16:58
    print; the stock gapped down 9.5% and the quote is live and tight."""
    now = exchange_at(datetime(2026, 9, 23, 9, 30, 20, tzinfo=ET))
    d = FakeData(trade=52.00, trade_ts=(now - timedelta(days=1)).replace(hour=16, minute=58),
                 bid=47.00, ask=47.20)
    assert _lp(d, fallback=51.0) == pytest.approx(47.10)


@pytest.mark.parametrize("now,ts,fresh", [
    (datetime(2026, 9, 23, 9, 35, tzinfo=ET), datetime(2026, 9, 23, 9, 30, 0, tzinfo=ET), True),
    (datetime(2026, 9, 23, 9, 35, tzinfo=ET), datetime(2026, 9, 23, 9, 29, 59, tzinfo=ET), False),
    (datetime(2026, 9, 23, 11, 0, tzinfo=ET), datetime(2026, 9, 23, 10, 45, 0, tzinfo=ET), True),
    (datetime(2026, 9, 23, 11, 0, tzinfo=ET), datetime(2026, 9, 23, 10, 44, 59, tzinfo=ET), False),
    (datetime(2026, 9, 23, 11, 0, tzinfo=ET), datetime(2026, 9, 22, 10, 59, 0, tzinfo=ET), False),
], ids=["at-open", "one-second-before-open", "15min-old", "15min-1s-old", "yesterday-same-time"])
def test_r2_13_trade_freshness_boundaries(exchange_at, now, ts, fresh):
    exchange_at(now)
    d = FakeData(trade=100.0, trade_ts=ts, bid=None)
    assert _lp(d, fallback=97.0) == (100.0 if fresh else 97.0)


# ---- R2-14..16: congress ----------------------------------------------------

def test_r2_14_latest_filing_sees_a_members_newer_leg():
    """Port (evalfixes F-C)."""
    from congress_scraper import Transaction, detect_herds
    import signal_aggregator as sa

    def d(n):
        return (datetime.now() - timedelta(days=n)).strftime("%m/%d/%Y")
    txs = [Transaction(member="Jane Doe", chamber="house", state="CA", ticker="XYZ", asset_name="x",
                       direction="buy", amount_range="$1K", transaction_date=d(100), filing_date=d(80)),
           Transaction(member="John Roe", chamber="senate", state="NY", ticker="XYZ", asset_name="x",
                       direction="buy", amount_range="$1K", transaction_date=d(95), filing_date=d(75)),
           Transaction(member="Jane Doe", chamber="house", state="CA", ticker="XYZ", asset_name="x",
                       direction="buy", amount_range="$1K", transaction_date=d(20), filing_date=d(4))]
    herds = detect_herds(txs)
    assert len(herds) == 1
    assert herds[0].latest_filing == (datetime.now() - timedelta(days=4)).strftime("%Y-%m-%d")
    assert sa.herd_recency_days(herds[0]) == 4


def test_r2_14_an_opposite_direction_leg_does_not_refresh_the_herd():
    from congress_scraper import Transaction, detect_herds

    def d(n):
        return (datetime.now() - timedelta(days=n)).strftime("%m/%d/%Y")
    txs = [Transaction(member=m, chamber="house", state="CA", ticker="XYZ", asset_name="x",
                       direction="buy", amount_range="$1K", transaction_date=d(60), filing_date=d(50))
           for m in ("Jane Doe", "John Roe")]
    txs.append(Transaction(member="Jane Doe", chamber="house", state="CA", ticker="XYZ", asset_name="x",
                           direction="sell", amount_range="$1K", transaction_date=d(5), filing_date=d(2)))
    buy_herd = [h for h in detect_herds(txs) if h.direction == "buy"][0]
    assert buy_herd.latest_filing == (datetime.now() - timedelta(days=50)).strftime("%Y-%m-%d")


def test_r2_16_decayed_buy_herd_does_not_mask_a_fresh_sell_herd():
    """Port (evalfixes F-D)."""
    from congress_scraper import HerdSignal
    import signal_aggregator as sa
    stale_buy = HerdSignal(ticker="XYZ", direction="buy",
                           members=[{"name": "A One"}, {"name": "B Two"}, {"name": "C Three"}],
                           latest_filing=(date.today() - timedelta(days=75)).isoformat())
    fresh_sell = HerdSignal(ticker="XYZ", direction="sell",
                            members=[{"name": "D Four"}, {"name": "E Five"}],
                            latest_filing=(date.today() - timedelta(days=3)).isoformat())
    c = sa._congress_score("XYZ", [stale_buy, fresh_sell], {})
    assert c.score == pytest.approx(-0.4)
    assert "sell" in c.detail


def test_r2_16_every_herd_for_the_ticker_is_scored_and_clamped():
    from congress_scraper import HerdSignal
    import signal_aggregator as sa
    fresh = (date.today() - timedelta(days=3)).isoformat()
    buy = HerdSignal(ticker="XYZ", direction="buy", members=[{"name": "A One"}, {"name": "B Two"}],
                     latest_filing=fresh)
    sell = HerdSignal(ticker="XYZ", direction="sell", members=[{"name": "D Four"}, {"name": "E Five"}],
                      latest_filing=fresh)
    other = HerdSignal(ticker="QQQ", direction="buy", members=[{"name": "Z"}] * 5, latest_filing=fresh)
    assert sa._congress_score("XYZ", [buy, sell, other], {}).score == pytest.approx(0.0)
    big = [HerdSignal(ticker="XYZ", direction="buy", members=[{"name": f"M {i}"} for i in range(5)],
                      latest_filing=fresh) for _ in range(3)]
    assert sa._congress_score("XYZ", big, {}).score == pytest.approx(1.0)
    assert sa._congress_score("XYZ", [other], {}).score == 0.0


# ---- R2-15: the daily bar request keeps yesterday's bar --------------------

class SipBars:
    """StockHistoricalDataClient.get_stock_bars on the Basic data plan, with
    the server rules alpaca-py does not model:
      - the feed defaults to SIP; an explicit `end` inside the last 15 minutes
        on SIP is refused 403 'subscription does not permit querying recent
        SIP data' (IEX is allowed);
      - with no `end`, the server uses now-15min;
      - daily bars are stamped 00:00 America/New_York; today's (forming) bar
        exists once the session has started.
    Also serves a fresh latest trade so market-hours pricing works.
    `now` (tz-aware) pins the server's clock; without it the real clock is used.
    `holiday` removes a weekday session; `no_today` serves no bar for today yet
    (the first minutes after the open, behind the 15-minute delay)."""
    def __init__(self, today_et=None, now=None, holiday=None, no_today=False):
        self.now, self.no_today = now, no_today
        self.today_et = today_et or (now.astimezone(ET).date() if now else datetime.now(ET).date())
        self.days = [d for d in (self.today_et - timedelta(days=i) for i in range(45, -1, -1))
                     if d.weekday() < 5 and d != holiday]
        self.requests = []

    @staticmethod
    def _aware(d):
        if isinstance(d, str):
            d = datetime.fromisoformat(d)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)

    def get_stock_bars(self, req):
        from alpaca.data.enums import DataFeed
        from alpaca.data.models import Bar
        self.requests.append(req)
        now = self.now.astimezone(timezone.utc) if self.now else datetime.now(timezone.utc)
        recent_limit = now - timedelta(minutes=15)
        session_open = datetime(self.today_et.year, self.today_et.month, self.today_et.day, 9, 30, tzinfo=ET)
        if req.end is not None and req.feed in (None, DataFeed.SIP) and self._aware(req.end) > recent_limit:
            raise api_error(403, 40310000, "subscription does not permit querying recent SIP data")
        start = self._aware(req.start)
        end = recent_limit if req.end is None else self._aware(req.end)
        out = []
        for i, d in enumerate(self.days):
            ts = datetime(d.year, d.month, d.day, tzinfo=ET)
            if d == self.today_et and (now < session_open or self.no_today):
                continue                     # today's bar does not exist before the open (or yet)
            if start <= ts <= end:
                c = 100.0 + i + (1.5 if i % 3 == 0 else -0.5)      # not monotonic: a real RSI
                out.append(Bar(req.symbol_or_symbols[0], {
                    "t": ts.astimezone(timezone.utc).isoformat(), "o": c, "h": c + 1.0,
                    "l": c - 1.0, "c": c, "v": 1000 + i, "n": 10, "vw": c}))
        return {req.symbol_or_symbols[0]: out}

    def get_stock_latest_trade(self, req):
        from alpaca.data.models import Trade
        sym = req.symbol_or_symbols[0]
        ts = (self.now or FrozenET.pinned or datetime.now(ET)) - timedelta(minutes=1)
        return {sym: Trade(sym, {"t": ts.astimezone(timezone.utc).isoformat(), "p": 150.0, "s": 100})}

    def get_stock_latest_quote(self, req):
        return {}

    @property
    def prev_session(self):
        return [d for d in self.days if d < self.today_et][-1]


def test_r3_2_fake_refuses_a_recent_sip_end_like_the_basic_plan():
    """The fake must be able to say no, or the test below proves nothing."""
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    fake = SipBars()
    start = (datetime.now(timezone.utc) - timedelta(days=36)).strftime("%Y-%m-%d")
    for end in (datetime.now(timezone.utc), datetime.now(timezone.utc) - timedelta(minutes=14)):
        with pytest.raises(APIError) as e:
            fake.get_stock_bars(StockBarsRequest(symbol_or_symbols=["AAA"], timeframe=TimeFrame.Day,
                                                 start=start, end=end))
        assert e.value.status_code == 403 and "recent SIP data" in str(e.value)
    ok = fake.get_stock_bars(StockBarsRequest(symbol_or_symbols=["AAA"], timeframe=TimeFrame.Day,
                                              start=start, end=datetime.now(timezone.utc), feed=DataFeed.IEX))
    assert ok["AAA"]
    assert fake.get_stock_bars(StockBarsRequest(symbol_or_symbols=["AAA"], timeframe=TimeFrame.Day,
                                                start=start))["AAA"]


class PinnedClock(datetime):
    """datetime whose now() is pinned in EVERY zone (FrozenET pins only New
    York). `pinned` is tz-aware; now() with no zone returns naive local time,
    as datetime.now() does."""
    pinned = None

    @classmethod
    def now(cls, tz=None):
        return cls.pinned.astimezone(tz) if tz is not None else cls.pinned.astimezone().replace(tzinfo=None)

    @classmethod
    def today(cls):
        return cls.now()

    @classmethod
    def utcnow(cls):
        return cls.pinned.astimezone(timezone.utc).replace(tzinfo=None)


R2_15_CASES = [
    # (pinned New York time, market_open, holiday, no_today). Today's bar exists
    # in session and at the close scan and must be dropped; everywhere else the
    # last bar is the previous session's and must be kept.
    (datetime(2026, 9, 23, 11, 0, tzinfo=ET), True, None, False),     # Wednesday, in session
    (datetime(2026, 9, 23, 16, 0, tzinfo=ET), False, None, False),    # Wednesday, the 16:00 close scan
    (datetime(2026, 9, 23, 8, 0, tzinfo=ET), False, None, False),     # Wednesday, before the open
    (datetime(2026, 9, 26, 11, 0, tzinfo=ET), False, None, False),    # Saturday
    (datetime(2026, 11, 26, 11, 0, tzinfo=ET), False, date(2026, 11, 26), False),  # Thanksgiving
    (datetime(2026, 9, 23, 9, 31, tzinfo=ET), True, None, True),      # open, no bar for today yet
    (datetime(2026, 9, 23, 20, 30, tzinfo=ET), False, None, False),   # evening: UTC is already Thursday
    (datetime(2026, 9, 24, 1, 0, tzinfo=ET), False, None, False),     # 01:00 ET: Los Angeles is still Wednesday
]


@pytest.mark.parametrize("when,market_open,holiday,no_today", R2_15_CASES,
                         ids=["wednesday-in-session", "wednesday-close-scan", "wednesday-pre-open", "saturday",
                              "thanksgiving", "open-no-bar-yet", "evening-utc-thursday", "0100-et-la-wednesday"])
def test_r2_15_last_close_is_the_previous_session_and_todays_bar_is_dropped(monkeypatch, when, market_open,
                                                                              holiday, no_today):
    """Round-3 finding 2 (a82d324): the daily-bar request carries NO end. end=now
    is refused 403 on the Basic plan, which cost every market-hours scan its
    technicals; the older end of 'yesterday' went out as 00:00Z and dropped
    yesterday's bar. With no end the last complete bar is the previous
    session's, and today's forming bar (if any) is dropped.

    Every clock is pinned: the pipeline's (both zones) and the fake server's
    (test-quality review, 2026-09-26). On the real clock, a weekend run had no
    forming bar, so "today's bar is kept" (R2-15b) passed on weekends; pinning
    only New York time to one Wednesday then made "drop the last bar, whatever
    its date" pass forever and went red from 2026-10-09 as the unpinned bar
    window slid past the fake's data. Four pinned scenarios now: today's bar
    exists in session and at the 16:00 close scan (market closed, the bar
    incomplete behind the 15-minute SIP delay), and must be dropped in both;
    pre-open and on Saturday the last bar is the previous session's and must
    be kept (the close-scan case: test-quality narrow re-review). The final
    review added a market holiday, the open before today's first bar exists,
    and two hours where the UTC or Los Angeles date differs from New York's."""
    import alpaca.data.historical as hist
    import sovereign_pipeline as sp
    PinnedClock.pinned = when
    monkeypatch.setattr(sp, "datetime", PinnedClock)
    fake = SipBars(now=when, holiday=holiday, no_today=no_today)
    monkeypatch.setattr(hist, "StockHistoricalDataClient", lambda *a, **k: fake)
    data = sp.get_stock_data("AAA", market_open=market_open)
    req = fake.requests[0]
    assert req.end is None, f"bars request carries end={req.end!r}"
    assert req.adjustment == Adjustment.ALL
    assert data.bars_30d, "no bars: the technicals were lost"
    assert data.bars_30d[-1]["date"] == str(fake.prev_session), "the last close is not the previous session's"
    assert all(b["date"] != str(fake.today_et) for b in data.bars_30d), "today's forming bar was kept"
    assert data.rsi_14 != 50.0 and data.atr_14 > 0, "technicals were not computed"
    if not market_open:
        assert data.current_price == data.bars_30d[-1]["close"]


# ---- R2-17 / R2-18: backtest ------------------------------------------------

def test_r2_17_backtest_bar_cache_is_versioned_by_adjustment():
    import bt_data
    assert bt_data.CACHE_DIR.parent == sovereign_config.STATE_DIR
    assert bt_data.CACHE_DIR.name != "bars_cache", "reads the raw-bar cache written before adjustment"
    assert "adj" in bt_data.CACHE_DIR.name.lower()
    assert bt_data._cache_path("aaa") == bt_data.CACHE_DIR / "AAA.json"


def test_r2_18_backtest_docstring_no_longer_claims_the_live_rules():
    import re
    import sovereign_backtest as bt
    doc = " ".join(bt.__doc__.split())
    assert not re.search(r"[-—]\s*the live rules", doc), "still claims to test the live rules"
    assert "the live rules" not in doc.lower(), "still claims to test the live rules"
    # The disclaimer must be about the EXIT logic; 'not the live composite'
    # elsewhere in the docstring does not count (mutant R2-18b).
    assert re.search(r"\bis not the live exit logic\b", doc, re.I), "the exit-logic disclaimer is gone"


# ---- R2-19: the GAP probes (each kills a mutant the old suite missed) -----

def test_r2_19_gap_F3b_single_empty_positions_response_still_fires_the_stop(sandbox, monkeypatch):
    track("AAA")
    fb = use(monkeypatch, FakeBroker())
    fb.hold("AAA", 10, 100.0, 85.0)
    fb.positions_script = [[]]
    ex.manage()
    assert fb.close_calls == ["AAA"]
    assert fb.calls.count("get_all_positions") == 2, "an empty list was not re-read"


def test_r2_19_gap_F5d_pending_buy_check_failure_does_not_abort_stops(sandbox, monkeypatch):
    track("AAA")
    track("PB", pending_buy={"since": hours_ago(0)})       # malformed: no ids at all
    fb = use(monkeypatch, FakeBroker())
    fb.hold("AAA", 10, 100.0, 85.0)
    ex.manage()
    assert fb.close_calls == ["AAA"] and "PB" in state_now(sandbox)


def test_r2_19_gap_F11e_pending_buy_filled_below_thesis_gets_the_rule_stop(sandbox, monkeypatch):
    write_run(sandbox.results, now_id(5), {"AAA": sig()}, [buy_thesis("AAA", entry=100.0, stop=92.0)])
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 100.0}))
    fb.plans[("AAA", "buy")] = Plan("never")
    fb.cancel_modes["AAA"] = "stuck"
    ex.execute()
    assert state_now(sandbox)["AAA"]["pending_buy"]
    fb.prices["AAA"] = 95.0
    fb.fill_order(fb.orders_for("AAA", "buy")[0].id, price=95.0)
    ex.manage()
    st = state_now(sandbox)["AAA"]
    assert st["entry"] == 95.0 and st["stop"] == round(95.0 * 0.92, 2)


def test_r2_19_gap_same_day_halt_blocks_entries_after_equity_recovers(sandbox, monkeypatch):
    risk_engine.HALT_FILE.write_text(json.dumps({"date": datetime.now().strftime("%Y-%m-%d")}))
    write_run(sandbox.results, now_id(5), {"AAA": sig()}, [buy_thesis("AAA")])
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 100.0}))
    ex.execute()
    assert fb.submitted == []


def test_r2_19_yesterdays_halt_does_not_block_today(sandbox, monkeypatch):
    risk_engine.HALT_FILE.write_text(json.dumps(
        {"date": (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")}))
    write_run(sandbox.results, now_id(5), {"AAA": sig()}, [buy_thesis("AAA")])
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 100.0}))
    ex.execute()
    assert [r.symbol for r in fb.submitted] == ["AAA"]


def test_r2_19_full_exit_is_logged_with_its_qty(sandbox, monkeypatch):
    track("AAA")
    fb = use(monkeypatch, FakeBroker())
    fb.hold("AAA", 10, 100.0, 85.0)
    ex.manage()
    s = sells(sandbox, "AAA")
    assert len(s) == 1 and s[0]["qty"] == pytest.approx(10.0) and s[0]["partial"] is False


# ==========================================================================
# 15. round 3 (R3-1 .. R3-19): the findings a82d324 claims to fix, tested
#     against its commit message and the module docstring.
# ==========================================================================

def working_partial_buy(fb, sym, filled=75.0, qty=150.0, price=100.0, stop=92.0, target=115.0,
                        now_price=None):
    """A buy that filled `filled` of `qty` shares at `price` and is STILL
    WORKING at the broker (partially_filled), tracked as a pending buy whose
    held shares the broker shows."""
    buy = fb.seed_order(sym, "buy", "partially_filled", minutes_ago=30, qty=qty, filled=filled, price=price)
    fb.hold(sym, filled, price, price if now_price is None else now_price)
    track(sym, entry=price, stop=stop, target=target,
          pending_buy={"order_id": buy.id, "client_order_id": buy.client_order_id, "since": hours_ago(0.5)},
          order_id=buy.id)
    return buy


def delivered(sb, text):
    return [a for a in sb.alerts if text.lower() in a[0].lower()]


# ---- R3-1: alert() can never raise ----------------------------------------

def test_r3_1_alert_on_a_full_disk_does_not_raise_and_still_sends(sandbox, monkeypatch):
    """The dedupe write fails (ENOSPC): the alert still goes out, undeduped."""
    import sovereign_alerts
    monkeypatch.setattr(sovereign_alerts, "save_json", enospc)
    for _ in range(2):
        assert sovereign_alerts.alert("Disk test", ["x"], level="critical") is None
    assert len(delivered(sandbox, "Disk test")) == 2, "a failed dedupe write must fail OPEN (send)"


@pytest.mark.parametrize("broken", ["load_json", "_send_discord", "_send_email"])
def test_r3_1_alert_survives_any_internal_failure(sandbox, monkeypatch, broken):
    import sovereign_alerts
    monkeypatch.setattr(sovereign_alerts, broken, lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    assert sovereign_alerts.alert("Robust", ["x"], level="critical") is None


def _real_alert_on_a_full_disk(sandbox, monkeypatch):
    import sovereign_alerts
    monkeypatch.setattr(ex, "alert", sovereign_alerts.alert)
    for mod in (sovereign_alerts, risk_engine, ex):
        monkeypatch.setattr(mod, "save_json", enospc)


@pytest.mark.parametrize("trigger", ["state-save", "null-price-elsewhere", "account-unreadable",
                                     "breaker-and-snapshot"])
def test_r3_1_full_disk_with_the_real_alert_never_blocks_the_stop(sandbox, monkeypatch, trigger):
    """Every alert manage raises before the sell loop goes through the real
    alert(), whose dedupe write fails on the same full disk. None may abort
    manage before the stop goes out."""
    track("STOP")
    fb = use(monkeypatch, FakeBroker())
    fb.hold("STOP", 10, 100.0, 85.0)
    if trigger == "null-price-elsewhere":
        track("NUL")
        fb.hold("NUL", 10, 100.0, 100.0)
        fb.null_price.add("NUL")
    elif trigger == "account-unreadable":
        fb.fail["get_account"] = [server_error() for _ in range(3)]
    elif trigger == "breaker-and-snapshot":
        fb.equity = 94000.0                     # trips the breaker: halt.json write fails too
    _real_alert_on_a_full_disk(sandbox, monkeypatch)
    try:
        ex.manage()
    except Exception as e:
        pytest.fail(f"manage raised {e!r} before the exit; close_calls={fb.close_calls}")
    assert "STOP" in fb.close_calls and sells(sandbox, "STOP")


# ---- R3-2: see test_r2_15_* and test_r3_2_fake_refuses_* above -------------

# ---- R3-3: an armed breakeven survives the pending buy resolving ------------

@pytest.mark.parametrize("fill,stop", [(100.0, 100.5), (104.0, round(104.0 * 1.005, 2)), (96.0, 100.5)])
def test_r3_3_resolving_a_buy_never_lowers_an_armed_breakeven(fill, stop):
    st = {"entry": 100.0, "stop": 100.5, "target": 130.0, "trail_armed": True, "notional": 1000.0,
          "pending_buy": {"order_id": "x"}}
    ex._resolve_filled_buy(st, ex.Outcome("partial", fill, 5.0, None))
    assert st["stop"] == stop and st["trail_armed"] is True and "pending_buy" not in st
    assert st["entry"] == fill and st["notional"] == round(fill * 5.0, 2)


def test_r3_3_breakeven_armed_while_the_buy_was_pending_is_kept_when_it_resolves(sandbox, monkeypatch):
    """Thesis 100/92; 5 of 10 filled at 100, the buy still working. Price 111
    arms breakeven (100.5). The DAY buy then expires with the 5 filled: the
    stop must stay at breakeven, not fall back to the 8% rule (92)."""
    fb = use(monkeypatch, FakeBroker())
    buy = working_partial_buy(fb, "BE", filled=5.0, qty=10.0, price=100.0, target=130.0, now_price=111.0)
    ex.manage()
    st = state_now(sandbox)["BE"]
    assert st["trail_armed"] and st["stop"] == 100.5 and st.get("pending_buy")
    fb.orders[buy.id].status = "expired"
    fb.prices["BE"] = 108.0
    ex.manage()
    st = state_now(sandbox)["BE"]
    assert "pending_buy" not in st and st["entry"] == 100.0
    assert st["stop"] == 100.5 and st["trail_armed"] is True, "armed breakeven reset by the buy resolving"
    assert fb.close_calls == []


# ---- R3-4: wash-trade protection and an asynchronous cancel -----------------

def test_r3_4_fake_rejects_a_close_while_our_buy_works():
    fb = FakeBroker()
    fb.hold("AAA", 5, 100.0, 90.0)
    buy = fb.seed_order("AAA", "buy", "partially_filled", qty=10, filled=5, price=100.0)
    with pytest.raises(APIError) as e:
        fb.close_position("AAA")
    assert e.value.status_code == 403 and "wash trade" in str(e.value)
    fb.cancel_order_by_id(buy.id)                         # 204: only pending_cancel so far
    with pytest.raises(APIError) as e:
        fb.close_position("AAA")
    assert "wash trade" in str(e.value), "pending_cancel still blocks the sell"
    assert fb.get_order_by_id(buy.id).status == OrderStatus.CANCELED
    assert fb.close_position("AAA").side == OrderSide.SELL


def test_r3_4_fake_rejects_a_buy_while_our_sell_works():
    fb = FakeBroker(prices={"AAA": 10.0})
    fb.seed_order("AAA", "sell", "accepted", qty=5)
    with pytest.raises(APIError) as e:
        fb.submit_order(MarketOrderRequest(symbol="AAA", notional=100.0, client_order_id="w",
                                           side=OrderSide.BUY, time_in_force=TimeInForce.DAY))
    assert e.value.status_code == 403 and "wash trade" in str(e.value)


def test_r3_4_held_pending_buy_is_canceled_and_confirmed_terminal_before_the_close(sandbox, monkeypatch):
    fb = use(monkeypatch, FakeBroker())
    buy = working_partial_buy(fb, "AAA", now_price=80.0)
    ex.manage()
    assert fb.cancel_calls == [buy.id]
    assert fb.wash_rejections == [], "the close was sent while our own buy was still working"
    assert fb.orders[buy.id].status == "canceled"
    s = sells(sandbox, "AAA")
    assert fb.close_calls == ["AAA"] and len(s) == 1 and s[0]["qty"] == pytest.approx(75.0)
    assert "AAA" not in state_now(sandbox) and not alerts_matching(sandbox, "Exit FAILED")


@pytest.mark.parametrize("mode", ["stuck", "raise", "refuse"])
def test_r3_4_buy_that_is_not_confirmed_terminal_blocks_the_close_and_says_so(sandbox, monkeypatch, mode):
    """A close now would be rejected as a wash trade. The stop stays armed,
    the pending buy stays tracked, and it is alerted; the next run retries."""
    fb = use(monkeypatch, FakeBroker())
    buy = working_partial_buy(fb, "AAA", now_price=80.0)
    fb.cancel_modes["AAA"] = mode
    ex.manage()
    assert fb.close_calls == [] and not sells(sandbox)
    st = state_now(sandbox)["AAA"]
    assert st["pending_buy"]["order_id"] == buy.id and st["stop"] == 92.0
    a = alerts_matching(sandbox, "Exit waiting on a buy cancel")
    assert a and a[0][2]["level"] == "critical"
    # the venue confirms the cancel before the next run
    fb.cancel_modes["AAA"] = "ok"
    fb.orders[buy.id].cancel_stuck = False
    ex.manage()
    assert fb.close_calls == ["AAA"] and fb.wash_rejections == []
    assert len(sells(sandbox, "AAA")) == 1 and "AAA" not in state_now(sandbox)


def test_r3_4_buy_canceled_with_nothing_filled_at_exit_time_frees_the_close(sandbox, monkeypatch):
    """Mutant R3-4g. The broker holds 75 AAA and the tracked buy is still
    working with nothing attributed to it. The stop fires; the cancel makes
    the buy terminal with nothing filled ('dead'), which clears the wash-trade
    block -- so the close goes out in the SAME run, not never."""
    fb = use(monkeypatch, FakeBroker())
    buy = fb.seed_order("AAA", "buy", "accepted", minutes_ago=30, qty=150.0)
    fb.hold("AAA", 75, 100.0, 80.0)
    track("AAA", pending_buy={"order_id": buy.id, "client_order_id": buy.client_order_id,
                              "since": hours_ago(0.5)}, order_id=buy.id)
    ex.manage()
    assert fb.cancel_calls == [buy.id] and fb.orders[buy.id].status == "canceled"
    assert not alerts_matching(sandbox, "Exit waiting on a buy cancel"), \
        "a buy confirmed dead still blocked the close"
    assert fb.close_calls == ["AAA"] and fb.wash_rejections == []
    assert len(sells(sandbox, "AAA")) == 1 and "AAA" not in state_now(sandbox)


def test_r3_4_fake_slow_cancel_confirms_on_the_third_read():
    fb = FakeBroker(prices={"AAA": 10.0})
    fb.cancel_modes["AAA"] = "slow"
    o = fb.seed_order("AAA", "buy", "accepted", qty=5)
    fb.cancel_order_by_id(o.id)
    assert [fb.get_order_by_id(o.id).status for _ in range(3)] == [
        OrderStatus.PENDING_CANCEL, OrderStatus.PENDING_CANCEL, OrderStatus.CANCELED]


def test_r3_4_manage_waits_for_a_slow_cancel_before_the_close(sandbox, monkeypatch):
    """Cancel is asynchronous: manage must POLL the buy until it is terminal
    (up to CANCEL_WAIT_S), not look once and give up or close regardless."""
    fb = use(monkeypatch, FakeBroker())
    buy = working_partial_buy(fb, "AAA", now_price=80.0)
    fb.cancel_modes["AAA"] = "slow"
    ex.manage()
    assert fb.orders[buy.id].status == "canceled" and fb.wash_rejections == []
    assert fb.close_calls == ["AAA"] and len(sells(sandbox, "AAA")) == 1


def test_r3_4_execute_waits_for_a_slow_cancel_of_an_unfilled_buy(sandbox, monkeypatch):
    write_run(sandbox.results, now_id(5), {"AAA": sig()}, [buy_thesis("AAA")])
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 100.0}))
    fb.plans[("AAA", "buy")] = Plan("never")
    fb.cancel_modes["AAA"] = "slow"
    ex.execute()
    assert fb.orders_for("AAA", "buy")[0].status == "canceled"
    assert "AAA" not in state_now(sandbox) and not buys(sandbox), "a confirmed-dead buy was recorded"


def test_r3_4_late_fill_during_the_cancel_is_resolved_as_ours_not_adopted(sandbox, monkeypatch):
    """The rest of the buy fills while the cancel is in flight. Those shares
    are OURS (this position, this stop), sold with the rest; nothing is left
    over for the next run to adopt as an outside position."""
    fb = use(monkeypatch, FakeBroker())
    working_partial_buy(fb, "AAA", now_price=80.0)
    fb.cancel_modes["AAA"] = "fills_first"
    ex.manage()
    s = sells(sandbox, "AAA")
    assert fb.close_calls == ["AAA"] and len(s) == 1 and s[0]["qty"] == pytest.approx(150.0)
    assert s[0]["entry"] == pytest.approx(90.0), "the exit was not priced against the combined fill"
    assert "AAA" not in fb.book and "AAA" not in state_now(sandbox)
    ex.manage()                                   # the next run finds nothing to adopt
    assert state_now(sandbox) == {} and len(sells(sandbox, "AAA")) == 1
    assert [t["action"] for t in trades(sandbox)] == ["sell"]


def test_r3_4_late_fill_without_a_stop_breach_keeps_the_position_ours(sandbox, monkeypatch):
    """The step-1 resolution path: the buy filled in full since the last run.
    The entry becomes the combined fill; the tracked entry is not re-adopted."""
    fb = use(monkeypatch, FakeBroker())
    buy = working_partial_buy(fb, "AAA", now_price=100.0)
    fb.fill_order(buy.id, price=102.0)
    ex.manage()
    st = state_now(sandbox)["AAA"]
    assert st["reason"] == "t" and "pending_buy" not in st
    assert st["entry"] == pytest.approx(101.0) and st["notional"] == pytest.approx(101.0 * 150, abs=0.01)


# ---- R3-5: execute's book includes every tracked entry ----------------------

def test_r3_5_tracked_positions_missing_from_an_empty_list_count_toward_max_positions(sandbox, monkeypatch):
    monkeypatch.setitem(RISK, "max_positions", 2)
    track("AAA")
    track("BBB")
    write_run(sandbox.results, now_id(5), {"CCC": sig()}, [buy_thesis("CCC")])
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 100.0, "BBB": 100.0, "CCC": 100.0}))
    fb.hold("AAA", 10, 100.0)
    fb.hold("BBB", 10, 100.0)
    fb.positions_script = [[], []]              # the glitch manage re-reads for, twice
    ex.execute()
    assert fb.submitted == [], "an empty positions response let execute blow through max_positions"


def test_r3_5_tracked_positions_missing_from_the_list_count_toward_the_sector_cap(sandbox, monkeypatch):
    monkeypatch.setitem(RISK, "sector_cap_pct", 0.20)
    risk_engine.record_entry("NVDA", 100.0, 15000.0, 92.0, 115.0, reason="t")   # filled, no pending_buy
    write_run(sandbox.results, now_id(5), {"AMD": sig(0.4)}, [buy_thesis("AMD")])
    fb = use(monkeypatch, FakeBroker(prices={"AMD": 100.0, "NVDA": 100.0}))
    fb.hold("NVDA", 150, 100.0)
    fb.positions_script = [[], []]
    ex.execute()
    assert [(r.symbol, r.notional) for r in fb.submitted] == [("AMD", 5000.0)]


# ---- R3-6: a partly filled working buy is priced at its fill ----------------

@pytest.mark.parametrize("fill", [95.0, 110.0])
def test_r3_6_working_partial_buy_is_recorded_and_stopped_from_its_fill(sandbox, monkeypatch, fill):
    """Thesis 100/92 (8%). Half fills at `fill` and the cancel fails. The
    held shares are priced at their fill and stopped 8% under it -- not 16%
    under a 110 fill, nor 3% under a 95 fill."""
    write_run(sandbox.results, now_id(5), {"AAA": sig()}, [buy_thesis("AAA", entry=100.0, stop=92.0)])
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 100.0}))
    fb.plans[("AAA", "buy")] = Plan("partial", after=1, frac=0.5, price=fill, then=None)
    fb.cancel_modes["AAA"] = "raise"
    ex.execute()
    st = state_now(sandbox)["AAA"]
    assert st.get("pending_buy"), "the working remainder is no longer tracked"
    assert st["entry"] == fill and st["thesis_entry"] == 100.0
    assert st["stop"] == round(fill * 0.92, 2)
    b = buys(sandbox, "AAA")[0]
    assert b["entry"] == fill and b["fill_price"] == fill and b["thesis_entry"] == 100.0


def test_r3_6_unknown_buy_filling_partway_later_is_restopped_from_its_broker_fill(sandbox, monkeypatch):
    """Mutant R3-6b. An UnknownOutcome buy was recorded at the thesis (100/92).
    Later runs find it still WORKING, 75 shares filled at 95 (the broker's
    average). manage must price those shares at 95 and widen the stop to the
    8% rule (87.40) -- execute never saw this fill, so only the re-anchor can.
    At 91 (4.2% under the fill) nothing is sold."""
    fb = use(monkeypatch, FakeBroker())
    buy = fb.seed_order("AAA", "buy", "partially_filled", minutes_ago=30, qty=150.0, filled=75.0,
                        price=95.0, client_order_id="sov-x-AAA")
    fb.hold("AAA", 75, 95.0, 91.0)
    fb.cancel_modes["AAA"] = "raise"
    track("AAA", entry=100.0, stop=92.0, target=115.0, entry_source="thesis",
          pending_buy={"order_id": None, "client_order_id": "sov-x-AAA", "since": hours_ago(0.5)})
    ex.manage()
    st = state_now(sandbox)["AAA"]
    assert st["pending_buy"]["order_id"] == buy.id, "the working remainder is no longer tracked"
    assert st["entry"] == 95.0 and st["stop"] == pytest.approx(87.4)
    assert fb.close_calls == [] and fb.cancel_calls == [], \
        "shares stopped 3.2% under their real fill (the thesis stop, tighter than the rule)"


def test_r2_2_held_shares_of_a_working_partial_buy_below_the_fill_rule_are_sold(sandbox, monkeypatch):
    """Mirror of the R2-2 test above: 8.5% under the real fill IS a stop."""
    write_run(sandbox.results, now_id(5), {"AAA": sig()}, [buy_thesis("AAA", entry=100.0, stop=92.0)])
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 100.0}))
    fb.plans[("AAA", "buy")] = Plan("partial", after=1, frac=0.5, price=95.0, then=None)
    fb.cancel_modes["AAA"] = "raise"
    ex.execute()
    fb.cancel_modes["AAA"] = "ok"
    fb.prices["AAA"] = 86.9
    ex.manage()
    assert fb.close_calls == ["AAA"] and fb.wash_rejections == []


# ---- R3-7: one trade exited in pieces is one loss --------------------------

def test_r3_7_partial_exit_plus_its_remainder_is_one_loss(sandbox):
    append_log(sandbox, {"action": "sell", "why": "TARGET", "pnl_pct": 0.12, "order_id": "w"},
               {"action": "sell", "why": "STOP (partial fill)", "pnl_pct": -0.15, "order_id": "p1",
                "partial": True},
               {"action": "sell", "why": "STOP", "pnl_pct": -0.16, "order_id": "p2", "partial": False})
    assert risk_engine.get_consecutive_losses() == 1


def test_r3_7_two_real_losses_still_count_two_with_a_partial_between(sandbox):
    append_log(sandbox, {"action": "sell", "why": "STOP", "pnl_pct": -0.08, "order_id": "a"},
               {"action": "sell", "why": "STOP (partial fill)", "pnl_pct": -0.15, "order_id": "p1",
                "partial": True},
               {"action": "sell", "why": "STOP", "pnl_pct": -0.16, "order_id": "p2"})
    assert risk_engine.get_consecutive_losses() == 2


def test_r3_7_a_stop_filled_in_two_orders_does_not_cut_sizing(sandbox, monkeypatch):
    """End to end: the close sells 4 of 10 and dies; the next run sells 6.
    One losing trade: the streak is 1 and the next buy is full size."""
    track("PART")
    fb = use(monkeypatch, FakeBroker(prices={"NEW": 100.0}))
    fb.hold("PART", 10, 100.0, 85.0)
    fb.plans[("PART", "sell")] = Plan("partial", after=1, frac=0.4, then="cancel", then_after=1)
    ex.manage()
    fb.plans[("PART", "sell")] = Plan("fill", after=1)
    ex.manage()
    assert len(sells(sandbox, "PART")) == 2
    assert risk_engine.get_consecutive_losses() == 1
    write_run(sandbox.results, now_id(5), {"NEW": sig()}, [buy_thesis("NEW")])
    ex.execute()
    assert [(r.symbol, r.notional) for r in fb.submitted] == [("NEW", 15000.0)]


# ---- R3-8: committed-but-unfilled buy notional is not spendable cash --------

def test_r3_8_open_broker_buy_notional_is_not_spendable(sandbox, monkeypatch):
    """Equity 100k, cash 40k, a $20k buy working at the broker. Spendable =
    40k - 20k - 10k reserve = 10k, not 30k."""
    write_run(sandbox.results, now_id(5), {"AAA": sig()}, [buy_thesis("AAA")])
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 100.0, "ZZZ": 50.0}, cash=40000.0))
    fb.seed_order("ZZZ", "buy", "accepted", minutes_ago=10, notional=20000.0)
    ex.execute()
    assert [(r.symbol, r.notional) for r in fb.submitted] == [("AAA", 10000.0)]


def test_r3_8_tracked_pending_buy_notional_is_not_spendable(sandbox, monkeypatch):
    """The same $20k as a tracked pending (unknown-outcome) buy the broker
    list does not show."""
    risk_engine.record_entry("ZZZ", 50.0, 20000.0, 46.0, 57.5, reason="t",
                             pending_buy={"order_id": None, "client_order_id": "sov-x-ZZZ",
                                          "since": hours_ago(0.2)})
    write_run(sandbox.results, now_id(5), {"AAA": sig()}, [buy_thesis("AAA")])
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 100.0, "ZZZ": 50.0}, cash=40000.0))
    ex.execute()
    assert [(r.symbol, r.notional) for r in fb.submitted] == [("AAA", 10000.0)]


@pytest.mark.parametrize("prices", [{}, {"stop_price": 50.0}], ids=["market", "stop"])
def test_minor_qty_only_open_buy_of_unknown_cost_stops_entries(sandbox, monkeypatch, prices):
    """Re-review minor (2026-09-25). A qty-only market buy has no price until it
    fills, so it committed $0 and all of the cash read as spendable. Sovereign
    submits only notional buys; this one came from somewhere else, as the
    July/August second trader's did. Spendable cash is unknown: no entries, and
    say why. A stop buy with no limit is the same once triggered: a market order
    that can fill anywhere above its trigger (order-safety review MINOR-1)."""
    write_run(sandbox.results, now_id(5), {"AAA": sig()}, [buy_thesis("AAA")])
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 100.0, "ZZZ": 50.0}, cash=40000.0))
    fb.seed_order("ZZZ", "buy", "accepted", minutes_ago=10, qty=400, **prices)
    ex.execute()
    assert fb.submitted == []
    assert any_alert_mentions(sandbox, "unknown cost"), [a[0] for a in sandbox.alerts]


@pytest.mark.parametrize("prices", [{"limit_price": 50.0}, {"limit_price": 50.0, "stop_price": 49.0}],
                         ids=["limit", "stop_limit"])
def test_minor_priced_qty_open_buy_is_not_spendable(sandbox, monkeypatch, prices):
    """400 sh at a $50 limit (a stop-limit pays at most its limit) commits
    $20k: spendable = 40k - 20k - 10k reserve = 10k, as for the $20k notional
    buy above."""
    write_run(sandbox.results, now_id(5), {"AAA": sig()}, [buy_thesis("AAA")])
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 100.0, "ZZZ": 50.0}, cash=40000.0))
    fb.seed_order("ZZZ", "buy", "accepted", minutes_ago=10, qty=400, **prices)
    ex.execute()
    assert [(r.symbol, r.notional) for r in fb.submitted] == [("AAA", 10000.0)]


def test_r3_8_unfilled_remainder_of_a_partly_filled_working_buy_is_not_spendable(sandbox, monkeypatch):
    """ZZZ: a $20k buy has filled $10k (100 @ 100, already out of cash) and
    the other $10k is still working. Cash 30k: spendable = 30k - 10k still
    committed - 10k reserve = 10k. The docstring/commit: 'committed-but-
    unfilled buy notional is taken out of spendable cash'."""
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 100.0}, cash=30000.0))
    buy = fb.seed_order("ZZZ", "buy", "partially_filled", minutes_ago=10, notional=20000.0,
                        filled=100, price=100.0)
    fb.hold("ZZZ", 100, 100.0)
    risk_engine.record_entry("ZZZ", 100.0, 20000.0, 92.0, 115.0, reason="t",
                             pending_buy={"order_id": buy.id, "client_order_id": buy.client_order_id,
                                          "since": hours_ago(0.2)}, order_id=buy.id)
    write_run(sandbox.results, now_id(5), {"AAA": sig()}, [buy_thesis("AAA")])
    ex.execute()
    got = [(r.symbol, r.notional) for r in fb.submitted]
    assert got and got[0][0] == "AAA" and got[0][1] <= 10000.0, \
        f"the working buy's unfilled $10k was treated as spendable: {got}"


# ---- R3-9: transient errors detected by type ---------------------------------

TRANSIENT = [
    requests.exceptions.SSLError("EOF occurred in violation of protocol"),
    requests.exceptions.ChunkedEncodingError("IncompleteRead(0 bytes read)"),
    requests.exceptions.ProxyError("Cannot connect to proxy"),
    requests.exceptions.ConnectTimeout("connect timed out"),
    requests.exceptions.ReadTimeout("read timed out"),
    requests.exceptions.ConnectionError("Connection aborted"),
    requests.exceptions.ContentDecodingError("bad gzip"),
    json.JSONDecodeError("Expecting value", "<html>502", 0),
    ConnectionResetError(104, "Connection reset by peer"),
    TimeoutError("timed out"),
    requests.HTTPError("503", response=SimpleNamespace(status_code=503)),
    api_error(429, 42910000, "rate limit exceeded"),
    api_error(500, 50010000, "internal server error"),
    api_error(502, 50200000, "bad gateway"),
    api_error(504, 50400000, "gateway timeout"),
]
CLEAN_REFUSALS = [
    api_error(400, 40010000, "invalid order"),
    api_error(403, 40310000, "insufficient buying power"),
    api_error(404, 40410000, "not found"),
    api_error(422, 42210000, "notional must be limited to 2 decimal places"),
    requests.HTTPError("403", response=SimpleNamespace(status_code=403)),
]


def test_r3_9_urllib3_protocol_error_is_transient():
    import urllib3
    assert ex._is_transient(urllib3.exceptions.ProtocolError("Connection aborted."))


@pytest.mark.parametrize("err", TRANSIENT, ids=lambda e: type(e).__name__ + str(getattr(e, "status_code", "") or ""))
def test_r3_9_transport_errors_are_transient(err):
    assert ex._is_transient(err)


@pytest.mark.parametrize("err", CLEAN_REFUSALS, ids=lambda e: type(e).__name__ + str(getattr(e, "status_code", "") or ""))
def test_r3_9_a_clean_4xx_is_not_transient(err):
    assert not ex._is_transient(err)


@pytest.mark.parametrize("err", [requests.exceptions.ChunkedEncodingError("IncompleteRead"),
                                 requests.exceptions.SSLError("EOF"),
                                 requests.exceptions.ProxyError("proxy reset")],
                         ids=["chunked", "ssl", "proxy"])
def test_r3_9_accepted_buy_whose_response_broke_then_lookup_404_is_unknown(sandbox, monkeypatch, err):
    """The POST was accepted, the response broke, and the order is not yet
    visible by client_order_id (404). That is UNKNOWN: recorded as pending,
    no further buys -- never 'rejected' followed by a buy of the next name."""
    write_run(sandbox.results, now_id(5), {"AAA": sig(0.5), "BBB": sig(0.4)},
              [buy_thesis("AAA"), buy_thesis("BBB")])
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 100.0, "BBB": 100.0}))
    fb.plans[("AAA", "buy")] = Plan("never")
    fb.submit_faults["AAA"] = [("accept_then_raise", err)]
    fb.fail["get_order_by_client_id"] = [not_found() for _ in range(3)]
    ex.execute()
    assert [r.symbol for r in fb.submitted] == ["AAA"], "bought the next candidate after an unknown outcome"
    st = state_now(sandbox)["AAA"]
    assert st["pending_buy"]["client_order_id"] == fb.submitted[0].client_order_id
    assert alerts_matching(sandbox, "UNKNOWN")


# ---- R3-10: a trade-log failure in _record_buy is alerted, not fatal ---------

def _fail_buy_log(monkeypatch):
    real = ex._log_trade

    def flaky(rec):
        if rec.get("action") == "buy":
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(rec)
    monkeypatch.setattr(ex, "_log_trade", flaky)


def test_r3_10_buy_log_failure_is_alerted_and_state_still_recorded(sandbox, monkeypatch):
    write_run(sandbox.results, now_id(5), {"AAA": sig(0.5), "BBB": sig(0.4)},
              [buy_thesis("AAA"), buy_thesis("BBB")])
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 100.0, "BBB": 100.0}))
    _fail_buy_log(monkeypatch)
    ex.execute()                                  # must not raise
    oid = fb.orders_for("AAA", "buy")[0].id
    st = state_now(sandbox)
    assert st["AAA"]["order_id"] == oid and st["AAA"]["entry"] == 100.0
    a = alerts_matching(sandbox, "Buy NOT in trade log: AAA")
    assert a and a[0][2]["level"] == "critical" and a[0][2].get("dedupe") is False
    assert oid in " ".join(a[0][1])
    assert [r.symbol for r in fb.submitted] == ["AAA", "BBB"], "the run was aborted by the log failure"
    assert "BBB" in st


def test_r3_10_unknown_buy_log_failure_still_records_the_pending_buy(sandbox, monkeypatch):
    write_run(sandbox.results, now_id(5), {"AAA": sig()}, [buy_thesis("AAA")])
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 100.0}))
    fb.submit_faults["AAA"] = [("raise", requests.ReadTimeout("read timed out"))]
    _fail_buy_log(monkeypatch)
    ex.execute()
    st = state_now(sandbox)["AAA"]
    assert st["pending_buy"]["client_order_id"] == fb.submitted[0].client_order_id
    a = alerts_matching(sandbox, "Buy NOT in trade log")
    assert a and fb.submitted[0].client_order_id in " ".join(a[0][1])


# ---- R3-11: Postgres memory writes inside the lock have timeouts -------------

@pytest.mark.parametrize("module", ["sovereign_memory", "sovereign_kg"])
def test_r3_11_memory_database_connection_has_connect_and_statement_timeouts(monkeypatch, module):
    import importlib.util
    seen = []
    monkeypatch.setitem(sys.modules, "psycopg2", SimpleNamespace(connect=lambda **kw: seen.append(kw)))
    spec = importlib.util.spec_from_file_location(f"_real_{module}", REPO / f"{module}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._connect()
    kw = seen[0]
    assert 0 < float(kw.get("connect_timeout") or 0) <= 10, kw
    import re
    m = re.search(r"statement_timeout\s*=\s*(\d+)", kw.get("options") or "")
    assert m and 0 < int(m.group(1)) <= 10000, kw


# ---- R3-12: ticker-less critical manage alerts are never deduped -------------

def test_r3_12_sandbox_alert_stub_dedupes_with_a_real_per_day_key(sandbox):
    """The stub is only worth something if it CAN suppress: a dedupe=True
    alert fires once per (title, level, day); dedupe=False always fires; a
    key recorded on another day does not suppress today's."""
    import hashlib
    for _ in range(3):
        ex.alert("Stub probe", ["x"], level="critical")
        ex.alert("Stub probe NODEDUP", ["x"], level="critical", dedupe=False)
    assert len(delivered(sandbox, "Stub probe")) == 1 + 3
    assert len(delivered(sandbox, "Stub probe NODEDUP")) == 3
    assert len(sandbox.alert_calls) == 6
    key = hashlib.sha256("Old day|critical".encode()).hexdigest()[:16]
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    sandbox.sent_file.write_text(json.dumps({"date": yesterday, "keys": [key]}))
    ex.alert("Old day", ["x"], level="critical")
    assert len(delivered(sandbox, "Old day")) == 1


def _twice_positions_outage(sb, mp, fb):
    fb.fail["get_all_positions"] = [server_error() for _ in range(6)]
    return "positions unreadable"


def _twice_unevaluated(sb, mp, fb):
    track("AAA")
    track("BBB")
    fb.hold("AAA", 10, 100.0, 100.0)
    fb.hold("BBB", 10, 100.0, 100.0)
    fb.null_price.add("AAA")
    ex.manage()
    fb.null_price = {"BBB"}                      # a DIFFERENT ticker later the same day
    return "stops NOT evaluated"


def _twice_state_save(sb, mp, fb):
    track("AAA")
    fb.hold("AAA", 10, 100.0, 100.0)
    mp.setattr(ex, "save_positions_state", enospc)
    return "state save FAILED"


def _twice_account(sb, mp, fb):
    fb.fail["get_account"] = [server_error() for _ in range(6)]
    return "account unreadable"


def _twice_breaker(sb, mp, fb):
    mp.setattr(ex, "check_circuit_breaker", lambda a: (_ for _ in ()).throw(OSError("halt.json")))
    return "circuit breaker check FAILED"


def _twice_corrupt(sb, mp, fb):
    sb.positions.write_text("{")
    return "CORRUPT"


def _twice_clock(sb, mp, fb):
    track("AAA")
    fb.hold("AAA", 10, 100.0, 85.0)
    fb.fail["get_clock"] = [server_error() for _ in range(6)]
    pin_exchange_time(mp, SATURDAY_11AM)
    return "clock unreadable"


@pytest.mark.parametrize("setup", [_twice_positions_outage, _twice_unevaluated, _twice_state_save,
                                   _twice_account, _twice_breaker, _twice_corrupt, _twice_clock],
                         ids=lambda f: f.__name__[7:])
def test_r3_12_a_second_outage_the_same_day_is_alerted_again(sandbox, monkeypatch, setup):
    """alert() dedupes per (title, level, day). These titles carry no ticker,
    so a per-day dedupe silences the second, separate event of the day while
    its stops are skipped again."""
    fb = use(monkeypatch, FakeBroker())
    text = setup(sandbox, monkeypatch, fb)          # _twice_unevaluated already ran once (AAA)
    already = len(delivered(sandbox, text))
    ex.manage()
    ex.manage()
    got = delivered(sandbox, text)
    assert len(got) - already >= 2, f"'{text}' was deduped: {len(got) - already} delivered in 2 runs"
    assert all(a[2].get("level") == "critical" for a in got)
    if setup is _twice_unevaluated:
        assert any("BBB" in " ".join(a[1]) for a in got), "the later ticker's unevaluated stop was silent"


def test_r3_12_a_second_blocked_manage_the_same_day_is_alerted_again(sandbox, monkeypatch):
    use(monkeypatch, FakeBroker())
    for _ in range(2):
        fh = _hold_lock(30)
        try:
            ex.manage()
        finally:
            fh.close()
    assert len(delivered(sandbox, "lock held")) == 2


def test_r3_12_no_ticker_less_critical_manage_alert_is_deduped():
    """Structural guard: in manage's code (_manage, _save, _blocked and the
    exit helpers) an alert whose title is a fixed string carries no ticker, so
    a per-day dedupe on it silences a second, different event."""
    src = (REPO / "sovereign_execute.py").read_text()
    tree = ast.parse(src)
    fns = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    offenders, seen = [], 0
    for name in ("_manage", "_save", "_blocked", "_finish_exit", "_partial_exit", "_void_buy"):
        for call in ast.walk(fns[name]):
            if not (isinstance(call, ast.Call) and getattr(call.func, "id", None) == "alert"):
                continue
            kws = {k.arg: k.value for k in call.keywords}
            level = kws.get("level")
            if not (isinstance(level, ast.Constant) and level.value == "critical"):
                continue
            if isinstance(call.args[0], ast.JoinedStr):
                continue                         # carries the ticker: per-ticker dedupe
            seen += 1
            d = kws.get("dedupe")
            if not (isinstance(d, ast.Constant) and d.value is False):
                offenders.append((name, call.lineno, ast.unparse(call.args[0])))
    assert seen >= 6, seen
    assert not offenders, offenders


# ---- R3-13: an unresolvable unknown buy reaches the staleness alert ----------

@pytest.mark.parametrize("hours,stale", [(19, True), (17, False)])
def test_r3_13_unresolvable_unknown_buy_staleness_boundary(sandbox, monkeypatch, hours, stale):
    fb = use(monkeypatch, FakeBroker())
    track("AAA", pending_buy={"order_id": None, "client_order_id": "sov-x-AAA", "since": hours_ago(hours)})
    fb.fail["get_order_by_client_id"] = [server_error() for _ in range(3)]
    ex.manage()
    assert bool(alerts_matching(sandbox, "Pending buy STALE")) == stale
    st = state_now(sandbox)["AAA"]
    assert st["pending_buy"]["client_order_id"] == "sov-x-AAA", "an unreadable lookup dropped the buy"
    assert not [t for t in trades(sandbox) if t["action"] == "buy_void"]


# ---- R3-14: breaker and snapshot failures are alerted (see R2-8 + R3-12) -----

def test_r3_14_breaker_failure_alert_is_critical_and_undeduped(sandbox, monkeypatch):
    track("AAA")
    fb = use(monkeypatch, FakeBroker())
    fb.hold("AAA", 10, 100.0, 85.0)
    monkeypatch.setattr(ex, "check_circuit_breaker", lambda a: (_ for _ in ()).throw(OSError("x")))
    ex.manage()
    a = alerts_matching(sandbox, "circuit breaker check FAILED")
    assert a and a[0][2]["level"] == "critical" and a[0][2].get("dedupe") is False
    assert fb.close_calls == ["AAA"]


def test_r3_14_snapshot_failure_alert_names_the_error_and_the_exit_still_goes(sandbox, monkeypatch):
    track("AAA")
    fb = use(monkeypatch, FakeBroker())
    fb.hold("AAA", 10, 100.0, 85.0)
    monkeypatch.setattr(ex, "write_account_snapshot", lambda *a, **k: 1 / 0)
    ex.manage()
    ex.manage()
    a = alerts_matching(sandbox, "snapshot failed")
    assert len(a) >= 1 and "division" in " ".join(a[0][1])
    assert fb.close_calls == ["AAA"]


# ---- R3-15: a 404 on an order is 'missing' -----------------------------------

@pytest.mark.parametrize("hours", [0.5, 19])
def test_r3_15_pending_exit_whose_order_404s_rearms_its_stop_now(sandbox, monkeypatch, hours):
    """a82d324: a pending exit whose order does not exist re-arms its stop at
    once -- not after 18h -- and the stop fires in the same run."""
    fb = use(monkeypatch, FakeBroker())
    fb.hold("HALT", 10, 100.0, 85.0)
    ghost = str(uuid.uuid4())                     # the broker 404s on it
    track("HALT", pending_exit={"order_id": ghost, "why": "STOP hit", "since": hours_ago(hours)})
    ex.manage()
    assert alerts_matching(sandbox, "Exit order not found")
    assert fb.close_calls == ["HALT"] and len(sells(sandbox, "HALT")) == 1
    assert sells(sandbox, "HALT")[0]["order_id"] != ghost
    assert "HALT" not in state_now(sandbox)


def test_r3_15_pending_exit_404_on_a_symbol_no_longer_held_is_not_kept_forever(sandbox, monkeypatch):
    """After a paper reset (or a bogus id) the order 404s every run and the
    symbol is not held. The entry must not linger and block the ticker."""
    fb = use(monkeypatch, FakeBroker(prices={"GONE": 90.0}))
    ghost = str(uuid.uuid4())
    track("GONE", pending_exit={"order_id": ghost, "why": "STOP hit", "since": hours_ago(1)})
    ex.manage()
    assert "GONE" not in state_now(sandbox), "a 404'd pending exit on an unheld symbol was kept"
    assert [t["action"] for t in trades(sandbox)] == ["reconcile_unresolved"]
    write_run(sandbox.results, now_id(5), {"GONE": sig()}, [buy_thesis("GONE", entry=90.0, stop=82.0,
                                                                      target=104.0)])
    ex.execute()
    assert [r.symbol for r in fb.submitted] == ["GONE"], "the ticker is still blocked"


def test_r3_15_pending_buy_whose_order_404s_is_voided(sandbox, monkeypatch):
    use(monkeypatch, FakeBroker(prices={"AAA": 100.0}))
    ghost = str(uuid.uuid4())
    track("AAA", pending_buy={"order_id": ghost, "client_order_id": "sov-x-AAA", "since": hours_ago(1)},
          order_id=ghost)
    ex.manage()
    assert "AAA" not in state_now(sandbox)
    v = [t for t in trades(sandbox) if t["action"] == "buy_void"]
    assert len(v) == 1 and v[0]["order_id"] == ghost and v[0]["ticker"] == "AAA"
    assert not sells(sandbox)


# ---- R3-16: a malformed state entry is a corrupt state file ------------------

MALFORMED = [None, [], "AAA", 5, {"stop": 92.0}, {"entry": 100.0}]


@pytest.mark.parametrize("junk", MALFORMED, ids=["null", "list", "str", "int", "no-entry", "no-stop"])
def test_r3_16_malformed_entry_stops_manage_with_an_alert_not_a_crash(sandbox, monkeypatch, junk):
    content = json.dumps({"AAA": {"entry": 100.0, "stop": 92.0, "target": 115.0, "trail_armed": False},
                          "JUNK": junk})
    sandbox.positions.write_text(content)
    fb = use(monkeypatch, FakeBroker())
    fb.hold("AAA", 10, 100.0, 85.0)
    try:
        ex.manage()
    except Exception as e:
        pytest.fail(f"manage crashed on a malformed entry: {e!r}")
    assert alerts_matching(sandbox, "CORRUPT")
    assert sandbox.positions.read_text() == content
    assert fb.close_calls == []


@pytest.mark.parametrize("junk", MALFORMED, ids=["null", "list", "str", "int", "no-entry", "no-stop"])
def test_r3_16_malformed_entry_stops_execute_before_any_order(sandbox, monkeypatch, junk):
    content = json.dumps({"JUNK": junk})
    sandbox.positions.write_text(content)
    write_run(sandbox.results, now_id(5), {"BBB": sig()}, [buy_thesis("BBB")])
    fb = use(monkeypatch, FakeBroker(prices={"BBB": 100.0}))
    ex.execute()
    assert fb.submitted == [] and alerts_matching(sandbox, "CORRUPT")
    assert sandbox.positions.read_text() == content


# ---- R3-17: a buy that never became a position is voided in the log ----------

def test_r3_17_unknown_buy_never_placed_is_voided_in_the_log(sandbox, monkeypatch):
    write_run(sandbox.results, now_id(5), {"AAA": sig()}, [buy_thesis("AAA")])
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 100.0}))
    fb.submit_faults["AAA"] = [("raise", requests.ReadTimeout("read timed out"))]
    ex.execute()
    coid = fb.submitted[0].client_order_id
    ex.manage()
    t = trades(sandbox)
    assert [r["action"] for r in t] == ["buy", "buy_void"]
    assert t[1]["client_order_id"] == coid == t[0]["client_order_id"] and t[1]["ticker"] == "AAA"


def test_r3_17_pending_buy_canceled_with_nothing_filled_is_voided_in_the_log(sandbox, monkeypatch):
    write_run(sandbox.results, now_id(5), {"AAA": sig()}, [buy_thesis("AAA")])
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 100.0}))
    fb.plans[("AAA", "buy")] = Plan("never")
    fb.cancel_modes["AAA"] = "stuck"
    ex.execute()
    oid = fb.orders_for("AAA", "buy")[0].id
    fb.cancel_order(oid)
    ex.manage()
    t = trades(sandbox)
    assert [r["action"] for r in t] == ["buy", "buy_void"]
    assert t[1]["order_id"] == oid == t[0]["order_id"]
    ex.manage()
    assert len([r for r in trades(sandbox) if r["action"] == "buy_void"]) == 1, "voided twice"


def test_r3_17_a_buy_that_filled_later_is_not_voided(sandbox, monkeypatch):
    write_run(sandbox.results, now_id(5), {"AAA": sig()}, [buy_thesis("AAA")])
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 100.0}))
    fb.plans[("AAA", "buy")] = Plan("never")
    fb.cancel_modes["AAA"] = "stuck"
    ex.execute()
    fb.fill_order(fb.orders_for("AAA", "buy")[0].id, price=100.0)
    ex.manage()
    assert not [r for r in trades(sandbox) if r["action"] == "buy_void"]


# ---- R3-18: re-anchoring alerts when it lowers a stop; breakeven re-armed -----

def test_r3_18_first_deploy_reanchor_that_lowers_a_stop_is_alerted(sandbox, monkeypatch):
    """Round-3 case, synthetic numbers: old-format state entry 300 (thesis) / stop 276, the
    broker's average is 290, price 272. The new entry widens the stop to the
    rule under 290 (266.80): no sell -- and it must not be silent."""
    track("COIN", entry=300.0, stop=276.0, target=345.0)
    fb = use(monkeypatch, FakeBroker())
    fb.hold("COIN", 10, 290.0, 272.0)
    ex.manage()
    st = state_now(sandbox)["COIN"]
    assert st["entry"] == 290.0 and st["stop"] == round(290.0 * 0.92, 2)
    assert fb.close_calls == []
    a = alerts_matching(sandbox, "Stop lowered on re-anchor: COIN")
    assert a, "a stop was lowered on re-anchor without an alert"
    text = " ".join(a[0][1])
    assert "276" in text and "266.8" in text and a[0][2].get("dedupe") is False
    assert "_stop_lowered" not in st, "the transient marker was persisted"


def test_r3_18_reanchor_that_keeps_or_raises_the_stop_is_silent(sandbox, monkeypatch):
    track("KEEP", entry=100.0, stop=92.0, target=130.0)
    fb = use(monkeypatch, FakeBroker())
    fb.hold("KEEP", 10, 110.0, 111.0)                # the fill was higher: 92 is wider than the rule
    ex.manage()
    assert state_now(sandbox)["KEEP"]["stop"] == 92.0
    assert not alerts_matching(sandbox, "Stop lowered")


def test_r3_18_armed_breakeven_is_rearmed_on_the_real_fill(sandbox, monkeypatch):
    """Round-3 case, synthetic numbers: armed at 180.90 off a 180 thesis entry; the real
    average is 183. The breakeven moves up to 183 * 1.005, never below the
    real fill."""
    track("TSM", entry=180.0, stop=180.9, target=230.0, trail_armed=True)
    fb = use(monkeypatch, FakeBroker())
    fb.hold("TSM", 10, 183.0, 190.0)
    ex.manage()
    st = state_now(sandbox)["TSM"]
    assert st["entry"] == 183.0 and st["stop"] == round(183.0 * 1.005, 2) and st["trail_armed"] is True
    assert not alerts_matching(sandbox, "Stop lowered")
    fb.prices["TSM"] = 183.5                         # under the real breakeven: it is a stop now
    ex.manage()
    assert fb.close_calls == ["TSM"]


# ---- R3-19: test gaps ----------------------------------------------------------

def test_r3_19_torn_line_mid_log_does_not_hide_later_exits_from_the_dedupe(sandbox):
    """Mutant R2-9b: if the dedupe stopped at the first unreadable line, an
    exit logged after it would be logged again by a reconcile."""
    (sandbox.state / "trade_log.jsonl").write_text(
        '{"action": "sell", "order_id": "o-1", "partial": false}\n{"action": "buy", "tick\n')
    assert ex._log_exit("AAA", "STOP", "o-2", 90.0, 10.0, {"entry": 100.0}, True, "manage")
    assert not ex._log_exit("AAA", "STOP", "o-2", 90.0, 10.0, {"entry": 100.0}, True, "reconciled")
    assert not ex._log_exit("AAA", "STOP", "o-1", 90.0, 10.0, {"entry": 100.0}, True, "reconciled")
    assert [r.get("order_id") for r in raw_log_records(sandbox) if r.get("action") == "sell"] == ["o-1", "o-2"]


def test_r3_19_non_404_lookup_error_never_drops_a_pending_buy(sandbox, monkeypatch):
    """Mutant R2-6e: only a 404 means 'never placed'."""
    fb = use(monkeypatch, FakeBroker())
    track("AAA", pending_buy={"order_id": None, "client_order_id": "sov-x-AAA", "since": hours_ago(1)})
    fb.fail["get_order_by_client_id"] = [api_error(403, 40310000, "forbidden") for _ in range(3)]
    ex.manage()
    assert "AAA" in state_now(sandbox) and not trades(sandbox)


@pytest.mark.parametrize("tif", [TimeInForce.GTC, TimeInForce.IOC, TimeInForce.OPG])
def test_r3_19_fake_refuses_a_notional_order_that_is_not_day(tif):
    fb = FakeBroker(prices={"AAA": 10.0})
    with pytest.raises(APIError) as e:
        fb.submit_order(MarketOrderRequest(symbol="AAA", notional=100.0, client_order_id="t",
                                           side=OrderSide.BUY, time_in_force=tif))
    assert e.value.status_code == 422 and fb.orders == {}


def test_r3_19_fake_tif_and_precision_rules():
    fb = FakeBroker(prices={"AAA": 10.0})
    with pytest.raises(APIError) as e:
        fb.submit_order(MarketOrderRequest(symbol="AAA", notional=100.123, client_order_id="p",
                                           side=OrderSide.BUY, time_in_force=TimeInForce.DAY))
    assert e.value.status_code == 422 and "2 decimal" in str(e.value)
    with pytest.raises(APIError):
        fb.submit_order(MarketOrderRequest(symbol="AAA", qty=1.5, client_order_id="f",
                                           side=OrderSide.BUY, time_in_force=TimeInForce.GTC))
    whole = fb.submit_order(MarketOrderRequest(symbol="AAA", qty=2, client_order_id="w",
                                               side=OrderSide.BUY, time_in_force=TimeInForce.GTC))
    assert whole.time_in_force == TimeInForce.GTC, "the order must echo the request's TIF"
    ok = fb.submit_order(MarketOrderRequest(symbol="BBB", notional=100.12, client_order_id="d",
                                            side=OrderSide.BUY, time_in_force=TimeInForce.DAY))
    assert ok.time_in_force == TimeInForce.DAY


def test_r3_19_buy_is_a_day_market_order_with_cent_precision(sandbox):
    fb = FakeBroker(prices={"AAA": 100.0})
    got = ex._submit_notional_buy(fb, "AAA", 1234.56789, "sov-x-AAA", datetime.now(timezone.utc))
    assert got is not None and got.time_in_force == TimeInForce.DAY
    req = fb.submitted[0]
    assert req.notional == 1234.57 and req.time_in_force == TimeInForce.DAY and req.qty is None


def test_r3_19_disk_full_exit_is_reported_as_an_exit_not_a_failure(sandbox, monkeypatch):
    """Mutant R2-8e: _finish_exit's save must go through _save. A raw save
    raising after the sell turns a filled exit into an 'Exit FAILED' alarm."""
    track("STOP")
    fb = use(monkeypatch, FakeBroker())
    fb.hold("STOP", 10, 100.0, 85.0)
    monkeypatch.setattr(ex, "save_positions_state", enospc)
    ex.manage()
    assert delivered(sandbox, "Exit: STOP")
    assert not alerts_matching(sandbox, "Exit FAILED")


def test_r3_19_buy_line_carries_the_sizing_math_and_the_thesis(sandbox, monkeypatch):
    """Docstring: every order is logged with the full reasoning chain --
    signal components, risk sizing math, thesis."""
    th = dict(buy_thesis("AAA"), reasoning="three-member herd plus momentum")
    write_run(sandbox.results, now_id(5), {"AAA": sig(0.42)}, [th])
    use(monkeypatch, FakeBroker(prices={"AAA": 100.0}))
    ex.execute()
    b = buys(sandbox, "AAA")[0]
    assert b["thesis"] == "three-member herd plus momentum"
    assert b["sizing"] and any("base 15%" in r for r in b["sizing"])
    assert b["composite"] == 0.42 and b["conviction"] == "medium"


class FrozenNow(datetime):
    pinned = None

    @classmethod
    def now(cls, tz=None):
        return cls.pinned if tz is None else cls.pinned.astimezone(tz)


@pytest.mark.parametrize("minutes,alerts", [(10.0, True), (9.99, False)])
def test_r3_19_lock_alert_boundary_is_inclusive_at_exactly_ten_minutes(sandbox, monkeypatch, minutes, alerts):
    """Mutant R2-4b (> instead of >=) survived because wall time always moves
    past 10:00.000 before the check. Pin it."""
    FrozenNow.pinned = datetime(2026, 9, 23, 11, 0, 0)
    monkeypatch.setattr(ex, "datetime", FrozenNow)
    fb = use(monkeypatch, FakeBroker())
    fh = open(ex.LOCK_FILE, "w")
    fh.write(json.dumps({"pid": 4242, "cmd": "execute",
                         "since": (FrozenNow.pinned - timedelta(minutes=minutes)).isoformat()}))
    fh.flush()
    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        ex.manage()
    finally:
        fh.close()
    assert bool(alerts_matching(sandbox, "lock held")) == alerts and fb.calls == []


# ---- minors 2026-09-26: tests for the mutants round 4 left MISSED -----------

def test_minor_close_waits_for_a_pending_buy_known_only_by_client_id(sandbox, monkeypatch):
    """Mutant R3-4i. The broker holds 75 AAA, below its stop. The tracked buy
    has no order id yet (its submit outcome was unknown) and every client-id
    lookup fails, so whether it is still working is unknown. A close now could
    meet our own working buy (a wash-trade rejection) or orphan a late fill:
    nothing is sold, the stop stays armed, and it is alerted."""
    fb = use(monkeypatch, FakeBroker())
    fb.seed_order("AAA", "buy", "partially_filled", minutes_ago=30, qty=150.0, filled=75.0,
                  price=100.0, client_order_id="sov-x-AAA")
    fb.hold("AAA", 75.0, 100.0, 80.0)
    track("AAA", entry=100.0, stop=92.0, target=115.0,
          pending_buy={"order_id": None, "client_order_id": "sov-x-AAA", "since": hours_ago(0.5)})
    fb.fail["get_order_by_client_id"] = [server_error() for _ in range(30)]
    ex.manage()
    assert fb.close_calls == [] and not sells(sandbox)
    assert state_now(sandbox)["AAA"]["pending_buy"]["client_order_id"] == "sov-x-AAA"
    assert alerts_matching(sandbox, "Exit waiting on a buy cancel")


def test_minor_working_partial_buy_commits_its_whole_order_within_the_run(sandbox, monkeypatch):
    """Mutant R3-6c. Half of AAA's $15k buy fills and the cancel fails, so the
    other half is still working and can still spend. Cash 30k less the 10k
    reserve leaves 20k: after AAA, BBB gets 5k. Recorded at its filled half,
    AAA would leave 12.5k looking spendable and BBB would take it."""
    write_run(sandbox.results, now_id(5), {"AAA": sig(0.5), "BBB": sig(0.4)},
              [buy_thesis("AAA"), buy_thesis("BBB")])
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 100.0, "BBB": 100.0}, cash=30000.0))
    fb.plans[("AAA", "buy")] = Plan("partial", after=1, frac=0.5, price=100.0, then=None)
    fb.cancel_modes["AAA"] = "raise"
    ex.execute()
    assert [(r.symbol, r.notional) for r in fb.submitted] == [("AAA", 15000.0), ("BBB", 5000.0)]
    assert state_now(sandbox)["AAA"]["notional"] == 15000.0


def test_minor_buys_in_one_run_spend_one_cash_balance(sandbox, monkeypatch):
    """Mutant R3-8c. Cash 30k less the 10k reserve leaves 20k. AAA takes 15k,
    so BBB gets the 5k that is left, not another 15k."""
    write_run(sandbox.results, now_id(5), {"AAA": sig(0.5), "BBB": sig(0.4)},
              [buy_thesis("AAA"), buy_thesis("BBB")])
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 100.0, "BBB": 100.0}, cash=30000.0))
    ex.execute()
    assert [(r.symbol, r.notional) for r in fb.submitted] == [("AAA", 15000.0), ("BBB", 5000.0)]


def test_minor_vanished_position_keeps_its_state_when_its_record_cannot_be_written(sandbox, monkeypatch, caplog):
    """Rule: log before delete (mutant R:log-before-delete-unresolved). The
    broker has no GONE and no sell for it. If the unresolved record cannot be
    written (disk full), the state entry stays for the next run. Deleted
    first, the position would be in neither the state nor the log."""
    track("GONE")
    use(monkeypatch, FakeBroker())
    real = ex._log_trade

    def full_disk(record):
        if record.get("action") == "reconcile_unresolved":
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(record)
    monkeypatch.setattr(ex, "_log_trade", full_disk)
    ex.manage()
    assert "GONE" in state_now(sandbox)
    assert "keeping its state for the next run" in caplog.text and "No space left on device" in caplog.text, \
        "the log-failure branch never ran"


def test_minor_correlation_bars_come_back_on_the_basic_plan(monkeypatch):
    """Mutant R3-2d. The correlation penalty's bars must come back on the Basic
    data plan. An `end` inside the last 15 minutes is refused 403, the except
    returns {}, and the penalty is silently off."""
    import alpaca.data.historical as hist
    fake = SipBars()
    monkeypatch.setattr(hist, "StockHistoricalDataClient", lambda *a, **k: fake)
    series = risk_engine._return_series(["AAA", "BBB"])
    assert fake.requests, "no bar request was made"
    assert len(series.get("AAA", [])) >= 20, sorted(series)


def test_minor_tracked_entry_with_a_pending_exit_counts_in_the_book(sandbox, monkeypatch):
    """Mutant R3-5c. A sell is out for AAA but not yet confirmed, and the
    broker's list no longer shows AAA. Until manage confirms the exit, AAA is
    still on the book (the sell can still fail): with max_positions 1, BBB
    does not go out."""
    monkeypatch.setitem(RISK, "max_positions", 1)
    track("AAA", pending_exit={"order_id": str(uuid.uuid4()), "since": hours_ago(0.1),
                               "why": "STOP hit"})
    write_run(sandbox.results, now_id(5), {"BBB": sig()}, [buy_thesis("BBB")])
    fb = use(monkeypatch, FakeBroker(prices={"BBB": 100.0}))
    ex.execute()
    assert fb.submitted == []


def test_minor_priced_qty_open_buy_counts_toward_its_sector(sandbox, monkeypatch):
    """Test-quality review #4 (2026-09-26). An open limit buy for 100 NVDA at
    $150 commits $15k to NVDA's sector as well as to cash. Sector cap 20%:
    AMD, same sector, gets the 5k left, not a full 15k."""
    monkeypatch.setitem(RISK, "sector_cap_pct", 0.20)
    write_run(sandbox.results, now_id(5), {"AMD": sig()}, [buy_thesis("AMD")])
    fb = use(monkeypatch, FakeBroker(prices={"AMD": 100.0, "NVDA": 150.0}))
    fb.seed_order("NVDA", "buy", "accepted", minutes_ago=10, qty=100, limit_price=150.0)
    ex.execute()
    assert [(r.symbol, r.notional) for r in fb.submitted] == [("AMD", 5000.0)]


def test_minor_execute_rereads_an_empty_positions_list_with_nothing_tracked(sandbox, monkeypatch):
    """Test-quality review #7 (2026-09-26). The re-read ran only when state was
    non-empty, so with nothing tracked one empty positions response hid every
    untracked holding: max_positions 2, the broker holds untracked XXX and YYY,
    and BBB went out as a third position."""
    monkeypatch.setitem(RISK, "max_positions", 2)
    write_run(sandbox.results, now_id(5), {"BBB": sig()}, [buy_thesis("BBB")])
    fb = use(monkeypatch, FakeBroker(prices={"BBB": 100.0, "XXX": 50.0, "YYY": 50.0}))
    fb.hold("XXX", 100, 50.0)
    fb.hold("YYY", 100, 50.0)
    fb.positions_script = [[]]
    ex.execute()
    assert fb.submitted == []


def test_minor_open_option_buy_is_unknown_cost_and_stops_entries(sandbox, monkeypatch):
    """Narrow re-review (2026-09-26). 60 contracts at a $5.00 limit commit
    about $30k, but qty x limit read $300: AAA got a full $15k and cash went
    negative once both filled. An option's limit is per share of a contract,
    so its cost is unknown: no entries, and say why."""
    write_run(sandbox.results, now_id(5), {"AAA": sig()}, [buy_thesis("AAA")])
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 100.0}, cash=40000.0))
    fb.seed_order("AAPL261218C00200000", "buy", "accepted", minutes_ago=10, qty=60,
                  limit_price=5.0, asset_class="us_option")
    ex.execute()
    assert fb.submitted == []
    assert any_alert_mentions(sandbox, "unknown cost"), [a[0] for a in sandbox.alerts]


def test_minor_open_buy_on_a_held_symbol_counts_toward_its_sector(sandbox, monkeypatch):
    """Narrow re-review (2026-09-26). NVDA is held ($1.5k) and a limit buy for
    100 more at $150 is open. Sector cap 20%: the sector carries 1.5k + 15k,
    so AMD gets 3.5k. Before, the open buy was skipped for a held symbol and
    AMD got 15k: holding more of a sector loosened its cap."""
    monkeypatch.setitem(RISK, "sector_cap_pct", 0.20)
    write_run(sandbox.results, now_id(5), {"AMD": sig()}, [buy_thesis("AMD")])
    fb = use(monkeypatch, FakeBroker(prices={"AMD": 100.0, "NVDA": 150.0}))
    fb.hold("NVDA", 10, 150.0)
    fb.seed_order("NVDA", "buy", "accepted", minutes_ago=10, qty=100, limit_price=150.0)
    ex.execute()
    assert [(r.symbol, r.notional) for r in fb.submitted] == [("AMD", 3500.0)]


def test_minor_manage_rereads_an_empty_positions_list_with_nothing_tracked(sandbox, monkeypatch):
    """Narrow re-review (2026-09-26), the gap #7 closed in execute, in manage.
    Nothing is tracked; the broker holds XXX (cost 100, now 120, past the 115
    target an adopted position gets). One empty positions response used to
    skip the cycle: no adoption, no exit, an empty snapshot."""
    fb = use(monkeypatch, FakeBroker(prices={"XXX": 120.0}))
    fb.hold("XXX", 10, 100.0, 120.0)
    fb.positions_script = [[]]
    ex.manage()
    assert fb.close_calls == ["XXX"]


@pytest.mark.parametrize("raw", [None, "NaN", "inf", ""], ids=["null", "nan", "inf", "empty"])
def test_minor_position_of_unknown_value_stops_entries(sandbox, monkeypatch, raw):
    """Final re-review (2026-09-26). NVDA is held but the broker returns no
    market value for it, and a limit buy for 10 more is open. The fold used to
    read the holding as $0, and AMD (same sector, cap 20%) went out for $15k,
    taking the sector to 31.5%. Before the fold, sizing raised on the None,
    which failed closed only by accident. Now: no entries, and say why."""
    monkeypatch.setitem(RISK, "sector_cap_pct", 0.20)
    write_run(sandbox.results, now_id(5), {"AMD": sig()}, [buy_thesis("AMD")])
    fb = use(monkeypatch, FakeBroker(prices={"AMD": 100.0, "NVDA": 150.0}))
    fb.hold("NVDA", 100, 150.0)
    if raw is None:
        fb.null_price.add("NVDA")
    else:
        fb.raw_market_value["NVDA"] = raw      # a NaN passed float() and switched the sector cap off
    fb.seed_order("NVDA", "buy", "accepted", minutes_ago=10, qty=10, limit_price=150.0)
    ex.execute()
    assert fb.submitted == []
    assert any_alert_mentions(sandbox, "unknown value"), [a[0] for a in sandbox.alerts]


def test_minor_two_open_buys_on_one_symbol_count_toward_its_sector(sandbox, monkeypatch):
    """Final re-review (2026-09-26). Two open limit buys for NVDA (50 at $150
    each), nothing held: one book entry worth 7.5k + 7.5k. Sector cap 20%: AMD
    gets 5k. Folding only into broker-held symbols would count the second buy
    at $0 and give AMD 12.5k."""
    monkeypatch.setitem(RISK, "sector_cap_pct", 0.20)
    write_run(sandbox.results, now_id(5), {"AMD": sig()}, [buy_thesis("AMD")])
    fb = use(monkeypatch, FakeBroker(prices={"AMD": 100.0, "NVDA": 150.0}))
    fb.seed_order("NVDA", "buy", "accepted", minutes_ago=10, qty=50, limit_price=150.0)
    fb.seed_order("NVDA", "buy", "accepted", minutes_ago=9, qty=50, limit_price=150.0)
    ex.execute()
    assert [(r.symbol, r.notional) for r in fb.submitted] == [("AMD", 5000.0)]


@pytest.mark.parametrize("ac,notional,qty,limit,expected", [
    ("us_equity", "1500", None, None, 1500.0),
    ("us_equity", None, "10", "50", 500.0),
    ("crypto", None, "2", "100", 200.0),
    ("us_option", None, "60", "5", None),
    (None, None, "1", "2.5", None),              # multi-leg option parent: no asset class
    ("crypto_perp", None, "1", "100", None),
    ("us_equity", None, "1", "-2.5", None),      # a net credit is not a commitment
    ("us_equity", None, "10", None, None),       # qty-only market buy
    ("us_equity", "-100", None, None, None),     # a negative notional is not a commitment
])
def test_minor_committed_prices_only_equity_and_crypto(ac, notional, qty, limit, expected):
    from alpaca.trading.enums import AssetClass
    o = SimpleNamespace(asset_class=AssetClass(ac) if ac in ("us_equity", "us_option", "crypto", "crypto_perp") else ac,
                        notional=notional, qty=qty, limit_price=limit, stop_price=None)
    assert ex._committed(o) == expected


def test_minor_manage_reread_failure_alerts_and_writes_no_snapshot(sandbox, monkeypatch):
    """Final failure-modes review (2026-09-26). The first positions read comes
    back empty and every re-read fails. That is not an empty book: manage must
    say stops are not enforced and must not write a snapshot claiming nothing
    is held."""
    fb = use(monkeypatch, FakeBroker(prices={"XXX": 120.0}))
    fb.hold("XXX", 10, 100.0, 120.0)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            return []
        raise server_error()
    fb.get_all_positions = flaky
    ex.manage()
    assert calls["n"] >= 2, "the empty first read was never re-read"
    assert any_alert_mentions(sandbox, "positions unreadable"), [a[0] for a in sandbox.alerts]
    assert not (sandbox.state / "account_snapshot.json").exists()
    assert fb.close_calls == []


def test_minor_partly_filled_open_buy_on_a_held_symbol_adds_only_its_remainder(sandbox, monkeypatch):
    """Final test-quality review (2026-09-26). NVDA holds 60 (10 plus 50 filled
    from the working buy) = 9k; the buy's unfilled remainder is 7.5k. Cap 20%:
    the sector carries 16.5k and AMD gets 3.5k. Adding the full 15k would
    double-count the filled 7.5k."""
    monkeypatch.setitem(RISK, "sector_cap_pct", 0.20)
    write_run(sandbox.results, now_id(5), {"AMD": sig()}, [buy_thesis("AMD")])
    fb = use(monkeypatch, FakeBroker(prices={"AMD": 100.0, "NVDA": 150.0}))
    fb.hold("NVDA", 60, 150.0)
    fb.seed_order("NVDA", "buy", "partially_filled", minutes_ago=10, qty=100, limit_price=150.0,
                  filled=50, price=150.0)
    ex.execute()
    assert [(r.symbol, r.notional) for r in fb.submitted] == [("AMD", 3500.0)]


def test_minor_open_buy_on_a_held_symbol_is_not_a_second_position(sandbox, monkeypatch):
    """Final test-quality review (2026-09-26). max_positions 2: NVDA held with
    an open buy for more is ONE position, so AAA may go out."""
    monkeypatch.setitem(RISK, "max_positions", 2)
    write_run(sandbox.results, now_id(5), {"AAA": sig()}, [buy_thesis("AAA")])
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 100.0, "NVDA": 150.0}))
    fb.hold("NVDA", 10, 150.0)
    fb.seed_order("NVDA", "buy", "accepted", minutes_ago=10, qty=100, limit_price=150.0)
    ex.execute()
    assert [r.symbol for r in fb.submitted] == ["AAA"]


def test_minor_execute_rereads_an_empty_positions_list(sandbox, monkeypatch):
    """Mutant R3-5b. Tracked entries join the book from state, so what the
    re-read protects is a position the broker holds that is not tracked here
    (bought outside Sovereign). max_positions 2: the broker holds tracked AAA
    and untracked XXX, and the first positions read comes back empty. Read
    once, the book is AAA alone and BBB goes out as a third position."""
    monkeypatch.setitem(RISK, "max_positions", 2)
    track("AAA")
    write_run(sandbox.results, now_id(5), {"BBB": sig()}, [buy_thesis("BBB")])
    fb = use(monkeypatch, FakeBroker(prices={"AAA": 100.0, "BBB": 100.0, "XXX": 50.0}))
    fb.hold("AAA", 100, 100.0)
    fb.hold("XXX", 100, 50.0)
    fb.positions_script = [[]]
    ex.execute()
    assert fb.submitted == []
