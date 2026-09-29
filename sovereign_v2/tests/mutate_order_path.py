"""Mutation check for tests/test_order_path.py: put each bug back in a scratch
copy of the repo and confirm the tests written for it go red. A test that
stays green with its fix removed is decorative.

Every mutation below re-creates a plausible real bug -- most of them are the
original buggy line, or the round-1 version the adversarial review flagged --
never a syntax break. They are grouped by:

  F1..F15   the round-1 adversarial findings
  R2-1..18  the round-2 findings, one group per finding
  R3-1..19  the round-3 findings (a82d324), one group per finding; R3-19 (the
            test gaps) folds in the scratch round-2 check (mut_r2.py) and the
            mutants round 3 reported MISSED: R2-6e, R2-9b, R2-8e, R2-18b,
            R:day-tif, R:log-sizing, R:log-thesis, R2-4b
  R:...     the rules in sovereign_execute.py's module docstring

A mutation normally makes one exact-text edit. One whose bug is guarded
twice (a check and its backstop, e.g. R2-1a/b) names MULTI as its file and
lists every edit; a single edit there would be an equivalent mutant.

Every pattern is exact text of the CURRENT code (a82d324). Where a later round
rewrote the code an earlier mutation targeted, the mutation re-creates the same
bug in the new code (often the previous round's version of the line).
  LP/BA/CD  live_price, split/dividend-adjusted bars, congress decay
  K:...     earlier fixes the old runner already guarded (kept)

    python3 tests/mutate_order_path.py              # all mutations
    python3 tests/mutate_order_path.py F1 LP        # only ids starting with these
    python3 tests/mutate_order_path.py -j 4         # parallel workers (default: cpus)

Verdicts: CAUGHT (a selected test went red), MISSED (all green: the tests
cannot see this bug), NOAPPLY (the pattern is not in the code exactly once:
the code moved and the mutation must be re-pointed), INVALID (the mutated
file does not compile: a syntax break proves nothing), NOSELECT, ERROR.
Exit 1 if any mutation is not CAUGHT, or if the unmutated baseline is red.

KNOWN_RED lists tests that fail on the unmutated tree (open findings). They are
deselected everywhere, so nothing here can lean on them -- and the findings
they pin cannot be mutation-checked until they are fixed.
"""
import argparse
import concurrent.futures as cf
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
PY = sys.executable
TESTS = "tests/test_order_path.py"

KNOWN_RED = [
    # Empty. The last entry (R3-8: the unfilled remainder of a partly filled,
    # still-working buy is spendable cash) was fixed in round 4 and is green.
    # Left listed, it was deselected everywhere and could catch nothing
    # (2026-09-26).
]

EX, RE, CFG, PL = "sovereign_execute.py", "risk_engine.py", "sovereign_config.py", "sovereign_pipeline.py"
SA, CS = "signal_aggregator.py", "congress_scraper.py"
MULTI = "<multi>"   # file slot of a mutation that needs several edits (see _edits)

# (id, what the bug is, file, original text, mutated text, -k selector expected to FAIL)
#
# Patterns track a82d324 (exact text), re-pointed 2026-09-26 to the round-4 code
# plus the minors branch. Where a later round rewrote the code a mutation
# targeted, the mutation re-creates the same bug in the new code.
MUTATIONS = [
    # ---------------- F1: exit state cleared on ACCEPTED, not FILLED ------------------
    ("F1a", "an accepted close is treated as a filled one", EX,
     '            order = _close(client, ticker)\n            out = _order_outcome(client, order.id)\n'
     '            if out.state == "filled":',
     '            order = _close(client, ticker)\n            out = _order_outcome(client, order.id)\n'
     '            if out.state not in ("dead", "partial"):',
     "accepted_but_unfilled_exit or pending_exit_is_not_readopted or pending_exit_logged_once"),
    ("F1b", "evaluate_exits re-sells / re-adopts an entry with a pending exit", RE,
     '    if st is not None and st.get("pending_exit"):\n        return',
     '    if False:\n        return',
     "pending_exit_is_not_readopted or evaluate_exits_skips_pending"),
    ("F1c", "a partially filled order counts as filled", EX,
     '            if st == "filled" and px:',
     '            if st in ("filled", "partially_filled") and px:',
     "partially_filled_exit_stays_pending or non_terminal_statuses_are_open"),
    ("F1d", "a pending exit that died is dropped instead of re-arming the stop", EX,
     '            elif out.state == "dead":\n                del st["pending_exit"]\n                _save(state)',
     '            elif out.state == "dead":\n                state.pop(sym)\n                _save(state)',
     "exit_order_that_dies_rearms"),
    ("F1e", "reconcile: a still-working broker sell is logged as a finished exit", EX,
     '                if out.state in ("filled", "partial"):\n                    _finish_exit(state, sym, "closed outside manage',
     '                if out.state != "dead":\n                    _finish_exit(state, sym, "closed outside manage',
     "vanished_position_with_a_working_sell_becomes_pending"),

    # ---------------- F2: _close recovery accepts bad evidence -------------------------
    ("F2a", "recovery accepts canceled/rejected/expired sells", EX,
     '             and (_status(o) not in TERMINAL or _qty(o) > 0) and _status(o) != "replaced"\n',
     '             and _status(o) != "replaced"\n',
     "close_recovery_rejects_bad_evidence"),
    ("F2b", "recovery accepts another symbol's sell (no symbol filter anywhere)", EX,
     '        status=QueryOrderStatus.ALL, symbols=[ticker], after=since, limit=50))\n'
     '    sells = [o for o in orders\n'
     '             if str(getattr(o.side, "value", o.side)) == "sell" and o.symbol == ticker\n',
     '        status=QueryOrderStatus.ALL, after=since, limit=50))\n'
     '    sells = [o for o in orders\n'
     '             if str(getattr(o.side, "value", o.side)) == "sell"\n',
     "close_recovery_rejects_bad_evidence"),
    ("F2c", "recovery window far too wide (an old sell is proof)", EX,
     "        order = _recent_sell(client, ticker, started - timedelta(minutes=2))",
     "        order = _recent_sell(client, ticker, started - timedelta(days=30))",
     "close_recovery_rejects_bad_evidence"),
    ("F2d", "recovery query uses the server default status=open (hides a filled sell)", EX,
     "        status=QueryOrderStatus.ALL, symbols=[ticker], after=since, limit=50))",
     "        symbols=[ticker], after=since, limit=50))",
     "close_that_raised_but_filled"),
    ("F2e", "close recovery removed: a raised close is always a failure", EX,
     "        order = _recent_sell(client, ticker, started - timedelta(minutes=2))\n        if order is not None:",
     "        order = None\n        if order is not None:",
     "close_that_raised_but_filled"),

    # ---------------- F3: one empty positions response wipes stops ---------------------
    ("F3a", "no per-symbol 'gone' confirmation", EX,
     "            gone, pos = _confirm_gone(client, sym)",
     "            gone, pos = True, None",
     "one_empty_positions_response or positions_list_empty_but_broker_confirms or two_empty_lists or gap_F3b"),
    ("F3b", "an empty positions list is not re-read before acting", EX,
     "        if not positions:\n"
     "            positions = list(_read(client.get_all_positions))   # one empty response is not evidence\n",
     "",
     "gap_F3b or one_empty_positions_response or positions_list_empty_but_broker_confirms or manage_rereads"),
    ("F3c", "any get_open_position error is read as 'gone'", EX,
     "        if _is_not_found(e):\n            return True, None\n        raise",
     "        return True, None",
     "confirm_gone_error_keeps_state"),
    ("F3d", "evaluate_exits deletes tracked symbols missing from the list (round-1 code)", RE,
     "    actions = []\n    for p in positions:\n        try:",
     "    actions = []\n    for gone in [s for s in state if s not in {p.symbol for p in positions}]:\n"
     "        state.pop(gone)\n    for p in positions:\n        try:",
     "evaluate_exits_saves_nothing or one_empty_positions_response"),
    ("F3e", "manage returns early on an empty book (the original order)", EX,
     "        positions = list(_read(client.get_all_positions))\n        if not positions:\n            positions",
     "        positions = list(_read(client.get_all_positions))\n        if not positions:\n"
     "            return\n        if not positions:\n            positions",
     "vanished_position_with_a_filled_sell or vanished_with_no_sell"),

    # ---------------- F4: clock failure silently skips stops ---------------------------
    ("F4a", "unreadable clock means 'closed' (the original _market_open)", EX,
     "        guess = now.weekday() < 5 and (9, 30) <= (now.hour, now.minute) < (16, 0)",
     "        guess = False",
     "clock_outage_in_market_hours"),
    ("F4b", "clock fallback ignores weekends", EX,
     "        guess = now.weekday() < 5 and (9, 30) <= (now.hour, now.minute) < (16, 0)",
     "        guess = (9, 30) <= (now.hour, now.minute) < (16, 0)",
     "clock_outage_on_a_weekend"),
    ("F4c", "no alert when exits depend on the clock guess", EX,
     '    if not certain:\n        alert("Sovereign: clock unreadable with exits pending",',
     '    if False:\n        alert("Sovereign: clock unreadable with exits pending",',
     "clock_outage_in_market_hours or clock_outage_on_a_weekend"),

    # ---------------- F5: log before delete; isolate each exit -------------------------
    ("F5a", "state deleted and saved BEFORE the exit is logged", EX,
     "    st = state.get(sym, {})\n    _log_exit(sym, why, order_id, fill_px, qty, st, paper, source)\n"
     "    state.pop(sym, None)\n    _save(state)\n",
     "    st = state.pop(sym, {})\n    _save(state)\n"
     "    _log_exit(sym, why, order_id, fill_px, qty, st, paper, source)\n",
     "log_failure_keeps_state"),
    ("F5b", "one failed exit ends the loop (remaining stops skipped)", EX,
     '            alert(f"Exit FAILED: {ticker}", [action["why"], str(e)[:300]],\n'
     '                  level="critical", dedupe=False)\n',
     '            alert(f"Exit FAILED: {ticker}", [action["why"], str(e)[:300]],\n'
     '                  level="critical", dedupe=False)\n            return\n',
     "one_failed_exit_does_not_skip or log_failure_keeps_state"),
    ("F5c", "a reconcile failure escapes and aborts the run", EX,
     '            log.error("Could not reconcile %s (%s) — keeping its state for the next run", sym, e)',
     '            log.error("Could not reconcile %s (%s) — keeping its state for the next run", sym, e)\n            raise',
     "confirm_gone_error_keeps_state"),
    ("F5d", "a pending-buy check failure escapes and aborts the run", EX,
     '            log.error("Resolving pending buy for %s failed: %s", sym, e)',
     '            log.error("Resolving pending buy for %s failed: %s", sym, e)\n            raise',
     "gap_F5d or pending_buy or unknown_buy"),

    # ---------------- F6: run lock and HTTP timeout ------------------------------------
    ("F6a", "lock never taken (overlapping runs)", EX,
     "            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)\n",
     "            pass\n",
     "overlapping_run_is_refused"),
    ("F6b", "manage runs without the lock", EX,
     'def manage():\n    with _run_lock("manage") as got:\n        if not got:\n'
     '            _blocked("manage")\n            return\n        _manage()',
     "def manage():\n    _manage()",
     "overlapping_run_is_refused or r2_4_manage_blocked"),
    ("F6c", "execute runs without the lock", EX,
     'def execute():\n    with _run_lock("execute") as got:\n        if not got:\n'
     '            _blocked("execute")\n            return\n        _execute()',
     "def execute():\n    _execute()",
     "overlapping_run_is_refused or blocked_execute_never_alerts"),
    ("F6d", "no HTTP timeout on the session (with_timeout is a no-op)", CFG,
     "            session.request = functools.partial(session.request, timeout=seconds)\n",
     "            pass\n",
     "every_trading_call_carries_an_http_timeout or cancel_carries_the_trading_timeout or data_client"),
    ("F6e", "timeout patched onto the wrong attribute (silent no-op)", CFG,
     '        session = getattr(client, "_session", None)',
     '        session = getattr(client, "session", None)',
     "every_trading_call_carries_an_http_timeout or cancel_carries_the_trading_timeout or data_client"),
    ("F6f", "exit dedupe by order id removed (a rerun logs one exit twice)", EX,
     "    if order_id and (str(order_id), partial) in _logged_order_ids():",
     "    if False:",
     "crash_after_logging_never_double_logs or log_exit_refuses_a_duplicate or pending_exit_logged_once"),
    ("F6g", "the trading client is built without with_timeout", EX,
     "    return with_timeout(TradingClient(key, secret, paper=paper), HTTP_TIMEOUT_S), paper",
     "    return TradingClient(key, secret, paper=paper), paper",
     "every_trading_call_carries_an_http_timeout or cancel_carries_the_trading_timeout"),

    # ---------------- F7: accepted-but-lookup-failed buy -------------------------------
    ("F7a", "an UNKNOWN buy outcome does not stop further buys", EX,
     '                  level="critical", dedupe=False)\n            break',
     '                  level="critical", dedupe=False)\n            continue',
     "unknown_buy_outcome_stops_further_buys or unknown_buy_is_recorded_as_pending"),
    ("F7b", "any failed lookup is read as 'the broker never saw it' (round-1 code)", EX,
     "            if _is_not_found(lookup_err) and not _is_transient(submit_err):\n                raise submit_err",
     "            if True:\n                raise submit_err",
     "unknown_buy_outcome_stops_further_buys or unknown_buy_is_recorded_as_pending or r2_7"),
    ("F7c", "no alert on an UNKNOWN buy", EX,
     '            alert(f"Buy outcome UNKNOWN: {ticker}", [str(e)[:300], "Recorded as pending; no further buys."],\n'
     '                  level="critical", dedupe=False)\n',
     "",
     "unknown_buy_outcome_stops_further_buys or unknown_buy_is_recorded_as_pending"),
    ("F7d", "an unknown/untracked position is adopted with no real stop", RE,
     '              "stop": round(price * (1 - RISK["stop_loss_pct"]), 2),',
     '              "stop": 0.0,',
     "unknown_buy_is_protected or untracked_position_is_adopted"),

    # ---------------- F8: reconciles with no sell reset the loss streak ----------------
    ("F8a", "a no-sell reconcile is logged as a sell (streak broken)", EX,
     '            _log_trade({"action": "reconcile_unresolved", "ticker": sym, "paper": paper,',
     '            _log_trade({"action": "sell", "ticker": sym, "paper": paper,',
     "no_sell_reconcile_does_not_reset or vanished_with_no_sell"),
    ("F8b", "a priceless reconciled sell breaks the streak", RE,
     '        if rec.get("source") == "reconciled" and rec.get("pnl_pct") is None:\n            continue\n',
     "",
     "priceless_reconciled_record"),
    ("F8c", "the same order id counted twice in the streak", RE,
     "        if oid and oid in seen:\n            continue",
     "        if False:\n            continue",
     "an_exit_logged_twice_counts_once"),

    # ---------------- F9: get_account outage -------------------------------------------
    ("F9a", "get_account failure crashes manage before any stop", EX,
     "    try:\n        account = _read(client.get_account)\n    except Exception as e:\n        account = None\n",
     "    try:\n        account = _read(client.get_account)\n    except ImportError as e:\n        account = None\n",
     "account_outage_still_enforces_stops"),
    ("F9b", "snapshot failure blocks exits", EX,
     "        try:\n            write_account_snapshot(account, positions, paper, state)\n"
     "        except Exception as e:  # reporting must never stop stop-enforcement\n"
     '            log.error("Account snapshot failed (exits continue): %s", e)\n'
     '            alert("Sovereign: account snapshot failed", [str(e)[:300], "Exits continue."],\n'
     '                  level="warning", dedupe=False)\n',
     "        write_account_snapshot(account, positions, paper, state)\n",
     "snapshot_failure_does_not_block"),
    ("F9c", "no alert when the account is unreadable", EX,
     '        alert("Sovereign: account unreadable", [str(e)[:300], "Stops still enforced."],\n'
     '              level="critical", dedupe=False)\n',
     "",
     "account_outage_still_enforces_stops"),

    # ---------------- F10: corrupt state file -------------------------------------------
    ("F10a", "a corrupt state file reads as {} (round-1 load_positions_state)", RE,
     "    if not POSITIONS_STATE.exists():\n        return {}\n    try:\n        state = json.loads(POSITIONS_STATE.read_text())",
     "    return load_json(POSITIONS_STATE, {})\n    try:\n        state = json.loads(POSITIONS_STATE.read_text())",
     "corrupt_state_stops_manage"),
    ("F10b", "valid JSON that is not an object is accepted", RE,
     "    if not isinstance(state, dict):\n",
     "    if False:\n",
     "corrupt_state_stops_manage"),
    ("F10c", "manage carries on with an empty book after StateCorrupt", EX,
     '        alert("Sovereign state file CORRUPT — stops not enforced", [str(e)], level="critical", dedupe=False)\n'
     '        return\n',
     '        alert("Sovereign state file CORRUPT — stops not enforced", [str(e)], level="critical", dedupe=False)\n'
     '        state = {}\n',
     "corrupt_state_stops_manage"),
    ("F10d", "save_json truncates in place (the original write_text)", CFG,
     '    tmp = path.with_name(f".{path.name}.tmp")\n    with open(tmp, "w") as f:',
     '    tmp = path\n    with open(tmp, "w") as f:',
     "save_json_is_atomic"),

    # ---------------- F11: unconfirmed fill recorded as a position ----------------------
    ("F11a", "an unfilled buy is recorded without pending_buy", EX,
     '    if pending:\n        extra["pending_buy"]',
     '    if False:\n        extra["pending_buy"]',
     "unfilled_buy_is_pending or pending_buy_is_never_reconciled or pending_buy_canceled_later"
     " or buy_whose_cancel_fails"),
    ("F11b", "reconcile does not skip pending buys (phantom exit)", EX,
     '                and not state[s].get("pending_buy") and not state[s].get("pending_exit")]',
     '                and not state[s].get("pending_exit")]',
     "pending_buy_is_never_reconciled"),
    ("F11c", "a canceled pending buy becomes a position", EX,
     'f"order {out.state} with nothing filled", paper)\n                del state[sym]',
     'f"order {out.state} with nothing filled", paper)\n                del st["pending_buy"]',
     "pending_buy_canceled_later"),
    ("F11d", "a pending buy that fills keeps its thesis target under the fill", EX,
     '    if st.get("target") and st["target"] <= st["entry"]:\n'
     '        st["target"] = round(st["entry"] * (1 + RISK["target_pct"]), 2)\n'
     '    st.pop("pending_buy", None)',
     '    st.pop("pending_buy", None)',
     "pending_buy_filled_later"),
    ("F11e", "a pending buy that fills below the thesis keeps a too-tight stop", EX,
     '    else:\n        st["stop"], _ = enforce_stop_rule(st["entry"], st["stop"])\n    if st.get("target")',
     '    else:\n        pass\n    if st.get("target")',
     "pending_buy_filled_later or gap_F11e"),

    # ---------------- F12: client_order_id reuse recovers an OLD order ------------------
    ("F12a", "recovery does not check the order's age", EX,
     "        if ((_status(order) in TERMINAL and _qty(order) == 0)\n"
     "                or (submitted and submitted < attempt_start - timedelta(seconds=60))):",
     "        if (_status(order) in TERMINAL and _qty(order) == 0):",
     "rerun_of_the_same_scan"),
    ("F12b", "client_order_id keyed on the clock, not the scan (a rerun double-buys)", EX,
     '    run_key = scan.get("run_id") or datetime.now().strftime("%Y%m%d_%H%M")',
     '    run_key = datetime.now().strftime("%Y%m%d_%H%M%S")',
     "rerun_of_the_same_scan or client_order_id_naming_the_run"),

    # ---------------- F13: recovered order's status ignored ------------------------------
    ("F13a", "recovery ignores a dead recovered order's status", EX,
     "        if ((_status(order) in TERMINAL and _qty(order) == 0)\n"
     "                or (submitted and submitted < attempt_start - timedelta(seconds=60))):",
     "        if (submitted and submitted < attempt_start - timedelta(seconds=60)):",
     "submit_recovery_returns_nothing_for_a_dead_order or recovered_dead_order"),
    ("F13b", "execute ignores a dead order after the fill poll", EX,
     '        if out.state == "dead":\n            log.warning("Buy %s order %s was canceled/rejected',
     '        if False:\n            log.warning("Buy %s order %s was canceled/rejected',
     "buy_rejected_after_acceptance or recovered_dead_order or unfilled_buy_is_canceled_after_fill_wait"),

    # ---------------- F14: fill polling ---------------------------------------------------
    ("F14a", "FILL_WAIT_S bound at def time", EX,
     "def _order_outcome(client, order_id, timeout: float = None) -> Outcome:",
     "def _order_outcome(client, order_id, timeout: float = FILL_WAIT_S) -> Outcome:",
     "fill_wait_is_read_at_call_time"),
    ("F14b", "the order is checked once, never polled", EX,
     "        if time.monotonic() >= deadline:\n            return out",
     "        if True:\n            return out",
     "fill_is_confirmed_by_polling or fill_wait_is_read_at_call_time"),
    ("F14c", "a market buy is assumed filled at the thesis price", EX,
     '        out = _order_outcome(client, order.id)\n        if out.state in ("open", "unknown", "replaced"):',
     '        out = Outcome("filled", float(order.filled_avg_price or entry), sizing["notional"] / entry, None)\n'
     '        if out.state in ("open", "unknown", "replaced"):',
     "unfilled_buy_is_pending or entry_is_the_fill or fill_is_confirmed_by_polling or unfilled_buy_is_canceled"),
    ("F14d", "expired not recognised as terminal (an expired order polls to 'open')", EX,
     'TERMINAL = {"canceled", "cancelled", "expired", "rejected"}',
     'TERMINAL = {"canceled", "cancelled", "rejected"}',
     "order_outcome_classifies_dead_orders or submit_recovery_returns_nothing or terminal_statuses"),

    # ---------------- F15: previously untested paths --------------------------------------
    ("F15a", "_read does not retry", EX,
     "def _read(fn, *args, attempts: int = 3, wait: float = 2.0, **kwargs):",
     "def _read(fn, *args, attempts: int = 1, wait: float = 2.0, **kwargs):",
     "transient_read_failures or read_gives_up or read_returns_on_first_success"),
    ("F15b", "_read backoff is flat, not growing", EX,
     "            time.sleep(wait * (i + 1))",
     "            time.sleep(wait)",
     "read_gives_up"),
    ("F15c", "reconcile_entry does not widen a stop the real entry made too tight", RE,
     '    else:\n        st["stop"], _ = enforce_stop_rule(st["entry"], st["stop"])\n    if st["stop"] < old_stop:',
     '    else:\n        pass\n    if st["stop"] < old_stop:',
     "reconcile_entry_widens or first_deploy_reanchor"),
    ("F15d", "reconcile_entry resets an armed breakeven to the 8% rule", RE,
     '    if st.get("trail_armed"):\n        # An armed breakeven only ever moves up: re-arm it on the real fill.\n'
     '        st["stop"] = max(st["stop"], round(st["entry"] * 1.005, 2))\n    else:\n'
     '        st["stop"], _ = enforce_stop_rule(st["entry"], st["stop"])',
     '    st["stop"], _ = enforce_stop_rule(st["entry"], st["stop"])',
     "reconcile_entry_rearms or armed_breakeven_is_rearmed"),
    ("F15e", "reconcile_entry does not lift a passed target", RE,
     '    if st.get("target") and st["target"] <= st["entry"]:\n        st["target"] = round(st["entry"] * (1 + RISK["target_pct"]), 2)\n    log.info(',
     "    log.info(",
     "reconcile_entry_keeps_a_wider_stop_and_lifts"),
    ("F15f", "reconcile_entry re-anchors on noise", RE,
     "    if broker_avg <= 0 or abs(st[\"entry\"] - broker_avg) / broker_avg <= 0.001:",
     "    if broker_avg <= 0:",
     "reconcile_entry_ignores_noise"),
    ("F15g", "entry never re-anchored to the broker average", RE,
     '        reconcile_entry(sym, st, float(p.avg_entry_price))',
     '        pass',
     "manage_reanchors_entry or reconcile_entry"),
    ("F15h", "target at or below the fill is kept (original line)", EX,
     "        tgt = target if target and target > entry else",
     "        tgt = target if target else",
     "target_at_or_below_the_fill"),
    ("F15i", "in-run buys not added to the book", EX,
     "        positions.append(SimpleNamespace(symbol=ticker, market_value=notional))\n",
     "",
     "buys_in_one_run_see_each_other or sector_cap_counts_buys"),
    ("F15j", "in-run buys added with no value (sector cap blind)", EX,
     "        positions.append(SimpleNamespace(symbol=ticker, market_value=notional))\n",
     "        positions.append(SimpleNamespace(symbol=ticker, market_value=0.0))\n",
     "sector_cap_counts_buys"),
    ("F15k", "CRISIS scan writes no halted scan (the 09:30 scan stays live)", PL,
     '        (RESULTS_DIR / f"scan_{run_id}.json").write_text(json.dumps(\n'
     '            {"run_id": run_id, "macro": vars(macro), "theses": [],\n'
     '             "halted": f"{macro.macro_regime} regime — no new positions"}, indent=2, default=str))\n',
     "",
     "crisis_scan_writes_an_empty_halted_scan"),
    ("F15l", "scan does not name its snapshot", PL,
     '        {"run_id": run_id, "signals_file": snap_path.name,',
     '        {"run_id": run_id,',
     "scan_names_its_own_snapshot"),
    ("F15m", "backend=sdk without a key silently falls back to the CLI", PL,
     "            if backend == \"sdk\":\n",
     "            if False:\n",
     "sdk_backend_without_a_key"),
    ("F15n", "pnl == 0 counted as a loss", RE,
     '            if float(rec["pnl_pct"]) < 0:',
     '            if float(rec["pnl_pct"]) <= 0:',
     "zero_pnl_exit_breaks_the_streak or breakeven_stop_is_not_a_loss"),
    ("F15o", "legacy snapshot fallback accepts any older snapshot", EX,
     "    if gap_min > 30:",
     "    if False:",
     "legacy_scan_without_pointer"),

    # ---------------- docstring rules ------------------------------------------------------
    ("R:live-refusal", "live confirmation defaults to 'yes'", EX,
     '    if not paper and os.environ.get("SOVEREIGN_LIVE_CONFIRM", "") != "yes":',
     '    if not paper and os.environ.get("SOVEREIGN_LIVE_CONFIRM", "yes") != "yes":',
     "live_account_refused"),
    ("R:breaker-units", "circuit breaker threshold in percent units (never trips)", RE,
     '    if last_equity > 0 and (equity / last_equity - 1) <= -RISK["daily_loss_halt_pct"]:',
     '    if last_equity > 0 and (equity / last_equity - 1) <= -RISK["daily_loss_halt_pct"] * 100:',
     "circuit_breaker_blocks_entries"),
    ("R:breaker-same-day", "a halt tripped earlier today is forgotten", RE,
     '    if halt.get("date") == today:\n        return True\n',
     "",
     "circuit_breaker or same_day_halt"),
    ("R:breaker-before-entry", "execute never consults the circuit breaker", EX,
     '    if check_circuit_breaker(account):\n        log.error("HALTED',
     '    if False:\n        log.error("HALTED',
     "circuit_breaker_blocks_entries or same_day_halt"),
    ("R:breaker-in-manage", "manage returns when the breaker is tripped (stops skipped)", EX,
     "            check_circuit_breaker(account)  # trips halt for today if breached",
     "            if check_circuit_breaker(account):\n                return",
     "circuit_breaker_does_not_stop_stop_losses"),
    ("R:market-open", "entries allowed in a closed market with a readable clock", EX,
     "    if not (is_open and certain):",
     "    if not (is_open or certain):",
     "no_entries_when_market_closed"),
    ("R:market-open-certain", "entries allowed on the exchange-hours guess", EX,
     "    if not (is_open and certain):",
     "    if not is_open:",
     "no_entries_when_clock_unreadable"),
    ("R:closed-queues", "manage fires exits with the market closed", EX,
     '    if not is_open:\n        log.info("Market closed — %d exit(s) queue',
     '    if False:\n        log.info("Market closed — %d exit(s) queue',
     "market_closed_queues_exits"),
    ("R:day-tif", "buys sent GTC (fractional/notional orders must be DAY)", EX,
     "            side=OrderSide.BUY, time_in_force=TimeInForce.DAY))",
     "            side=OrderSide.BUY, time_in_force=TimeInForce.GTC))",
     "r3_19_buy_is_a_day_market_order or entry_is_the_fill"),
    ("R:log-reasoning", "buy log drops the fill evidence", EX,
     '"fill_price": fill_px, "qty": qty,\n                    "fill_status": status, ',
     '"qty": qty,\n                    ',
     "unfilled_buy_is_pending or entry_is_the_fill or partial_buy_is_a_position"),
    ("R:log-sizing", "buy log drops the risk-sizing math", EX,
     '"sizing": sizing["reasons"], ',
     "",
     "buy_line_carries_the_sizing_math"),
    ("R:log-thesis", "buy log drops the thesis", EX,
     '"thesis": th.get("reasoning", ""), ',
     "",
     "buy_line_carries_the_sizing_math"),
    ("R:no-order-retry-buy", "the buy POST is wrapped in _read (retried)", EX,
     "        return client.submit_order(MarketOrderRequest(",
     "        return _read(client.submit_order, MarketOrderRequest(",
     "buy_that_really_failed"),
    ("R:no-order-retry-close", "the close DELETE is wrapped in _read (retried)", EX,
     "        return client.close_position(ticker)",
     "        return _read(client.close_position, ticker)",
     "close_that_really_failed"),
    ("R:reconcile-window", "reconcile accepts a sell from before the position opened", EX,
     "            order = _recent_sell(client, sym, opened.astimezone(timezone.utc))",
     "            order = _recent_sell(client, sym, opened.astimezone(timezone.utc) - timedelta(days=7))",
     "sell_from_before_the_position_opened"),
    ("R:no-sell-alert", "a position vanishing with no sell raises no critical alert", EX,
     '            alert(f"Position vanished with no sell: {sym}", ["Recorded as unresolved, not as a trade."],\n'
     '                  level="critical", dedupe=False)\n',
     "",
     "vanished_with_no_sell_is_a_record"),

    # ================= round-2 findings (24f9eb4), one group per finding =================

    # ---- R2-1: execute ignored tracked state --------------------------------------------
    # Since a82d324 every tracked entry and open buy is in the book, so
    # size_position's "already hold" guard backs up execute's `tracked` skip:
    # removing either one alone is an equivalent mutant (both were MISSED as
    # single edits). The bug -- buying a ticker already tracked -- needs both.
    ("R2-1a", "execute buys a tracked ticker (neither the tracked skip nor the already-held guard)", MULTI,
     [(EX, "        if ticker in tracked:", "        if False:"),
      (RE, "    if candidate in held:\n        return {\"notional\": 0.0, \"pct\": 0.0, \"reasons\": [f\"already hold",
           "    if False:\n        return {\"notional\": 0.0, \"pct\": 0.0, \"reasons\": [f\"already hold")],
     None,
     "r2_1 or pending_buy_from_an_earlier_run"),
    ("R2-1b", "'tracked' and the book are the broker's view only, not the state (round-1 view)", MULTI,
     [(EX, "    tracked = set(state) | {o.symbol for o in open_buys}", "    tracked = set(held)"),
      (EX, "    for sym, st in state.items():\n        pb = st.get(\"pending_buy\") or {}\n",
           "    for sym, st in {}.items():\n        pb = st.get(\"pending_buy\") or {}\n")],
     None,
     "r2_1 or r3_5"),
    ("R2-1c", "record_entry overwrites a tracked entry (drops its pending_exit)", RE,
     "    if ticker in state:\n        # execute skips",
     "    if False:\n        # execute skips",
     "record_entry_refuses or r2_1"),
    ("R2-1d", "tracked entries are not part of the book at all", EX,
     "    for sym, st in state.items():\n        pb = st.get(\"pending_buy\") or {}\n",
     "    for sym, st in []:\n        pb = st.get(\"pending_buy\") or {}\n",
     "tracked_pending_buy_counts_in_the_book or r3_5 or r3_8_tracked"),
    ("R2-1e", "open buy orders at the broker are not part of the book", EX,
     "        if o.symbol not in held:\n            positions.append(SimpleNamespace(symbol=o.symbol, market_value=full))",
     "        if False:\n            positions.append(SimpleNamespace(symbol=o.symbol, market_value=full))",
     "open_buy_order or r3_8_open_broker_buy"),
    ("R2-1f", "an open buy order counts with no value (sector cap blind)", EX,
     "positions.append(SimpleNamespace(symbol=o.symbol, market_value=full))",
     "positions.append(SimpleNamespace(symbol=o.symbol, market_value=0.0))",
     "open_buy_order_at_the_broker_counts_in_the_book"),
    ("R2-1g", "a tracked entry counts with no value (sector cap blind)", EX,
     "positions.append(SimpleNamespace(symbol=sym, market_value=notional))",
     "positions.append(SimpleNamespace(symbol=sym, market_value=0.0))",
     "tracked_pending_buy_counts_in_the_book or r3_5_tracked_positions_missing_from_the_list_count_toward_the_sector"),

    # ---- R2-2: partial fills --------------------------------------------------------------
    ("R2-2a", "a terminal order with a fill is 'dead' (partial fill lost)", EX,
     '                return Outcome("partial", px, q, None) if q > 0 and px else Outcome("dead", None, 0.0, None)',
     '                return Outcome("dead", None, 0.0, None)',
     "r2_2 or r2_3 or partially_filled_then_canceled"),
    ("R2-2b", "an unfilled buy is never canceled after FILL_WAIT_S", EX,
     "            try:\n                client.cancel_order_by_id(order.id)\n",
     "            try:\n                pass\n",
     "unfilled_buy_is_canceled_after_fill_wait or partially_filled_buy_is_canceled"),
    ("R2-2c", "a partial buy is recorded at the full sized notional", EX,
     '        notional = round(out.price * out.qty, 2) if filled else sizing["notional"]',
     '        notional = sizing["notional"]',
     "partial_buy_is_a_position_at_its_actual_fill or partially_filled_buy_is_canceled"),
    ("R2-2d", "a partial buy is not a position (recorded as pending at the thesis)", EX,
     '        filled = out.state in ("filled", "partial")',
     '        filled = out.state == "filled"',
     "r2_2"),
    ("R2-2e", "a partial exit is logged as a full exit and the state dropped", EX,
     '            elif out.state == "partial":\n                _partial_exit(state, ticker,',
     '            elif out.state == "partial":\n                _finish_exit(state, ticker,',
     "partial_exit_is_logged_for_the_shares_sold"),
    ("R2-2f", "a partially filled pending exit is never logged or re-armed", EX,
     '            elif out.state == "partial":\n'
     '                _partial_exit(state, sym, why, pe["order_id"], out.price, out.qty, paper, "manage")\n',
     "",
     "pending_exit_partially_filled_then_expired"),
    ("R2-2g", "after a partial exit the stop is not re-armed (pending_exit kept)", EX,
     '    st.pop("pending_exit", None)\n    _save(state)\n    alert(f"Partial exit',
     '    _save(state)\n    alert(f"Partial exit',
     "partial_exit or pending_exit_partially"),
    ("R2-2h", "partial and full exits of one order share a dedupe key", EX,
     'ids.add((rec["order_id"], rec.get("partial", False)))',
     'ids.add((rec["order_id"], False))',
     "log_exit_dedupes_a_partial_separately or partial_exit"),
    ("R2-2i", "manage: a pending buy canceled with a partial fill is never resolved", EX,
     '            elif out.state in ("filled", "partial"):\n                _resolve_filled_buy(st, out)',
     '            elif out.state == "filled":\n                _resolve_filled_buy(st, out)',
     "pending_buy_canceled_with_a_partial_fill or r3_3_breakeven_armed"),
    ("R2-2j", "manage: a resolved pending buy keeps the sized notional, not the filled one", EX,
     '    st["notional"] = round(out.price * out.qty, 2)\n',
     "",
     "pending_buy_canceled_with_a_partial_fill_keeps_its_own_stop or r3_3_resolving"),
    ("R2-2k", "held shares of a pending buy are skipped (round-1 evaluate_exits)", RE,
     '    if st is not None and st.get("pending_exit"):\n        return',
     '    if st is not None and (st.get("pending_exit") or st.get("pending_buy")):\n        return',
     "held_pending_buy or held_shares_of_a_working_buy"),
    ("R2-2l", "the working buy is not canceled before the close", EX,
     '                    try:\n                        client.cancel_order_by_id(pb["order_id"])',
     '                    try:\n                        pass',
     "held_shares_of_a_working_buy_get_their_stop or r3_4"),
    ("R2-2m", "execute measures the stop from the thesis entry, not the fill", EX,
     "        stop_price = round(entry * (1 - stop_pct), 2) if entry > 0 else 0",
     "        stop_price = round(thesis_entry * (1 - stop_pct), 2) if entry > 0 else 0",
     "partial_buy_is_a_position_at_its_actual_fill or entry_is_the_fill"),

    # ---- R2-3: non-terminal statuses ----------------------------------------------------------
    ("R2-3a", "stopped/suspended/done_for_day are terminal (round-1 DEAD)", EX,
     'TERMINAL = {"canceled", "cancelled", "expired", "rejected"}',
     'TERMINAL = {"canceled", "cancelled", "expired", "rejected", "done_for_day", "stopped", "suspended"}',
     "r2_3"),
    ("R2-3b", "'held' is terminal", EX,
     'TERMINAL = {"canceled", "cancelled", "expired", "rejected"}',
     'TERMINAL = {"canceled", "cancelled", "expired", "rejected", "held"}',
     "r2_3"),

    # ---- R2-4: busy lock / data-client timeout --------------------------------------------------
    ("R2-4a", "a blocked manage never alerts", EX,
     '    if name == "manage" and (age_min is None or age_min >= LOCK_ALERT_MIN):',
     "    if False:",
     "r2_4"),
    ("R2-4b", "lock alert fires only strictly after 10 min (boundary)", EX,
     '    if name == "manage" and (age_min is None or age_min >= LOCK_ALERT_MIN):',
     '    if name == "manage" and (age_min is None or age_min > LOCK_ALERT_MIN):',
     "manage_blocked_for_ten_minutes or lock_alert_boundary"),
    ("R2-4c", "an unknown lock holder is not alerted", EX,
     '    if name == "manage" and (age_min is None or age_min >= LOCK_ALERT_MIN):',
     '    if name == "manage" and (age_min is not None and age_min >= LOCK_ALERT_MIN):',
     "blocked_by_an_unknown_holder"),
    ("R2-4d", "a blocked execute alerts too (noise)", EX,
     '    if name == "manage" and (age_min is None or age_min >= LOCK_ALERT_MIN):',
     "    if (age_min is None or age_min >= LOCK_ALERT_MIN):",
     "blocked_execute_never_alerts"),
    ("R2-4e", "the lock holder is never recorded", EX,
     "                if os.write(fh.fileno(), note) != len(note):\n"
     '                    log.warning("Lock-holder note written short; a blocked run will report the holder as unknown")\n',
     "",
     "lock_records_its_holder"),
    ("R2-4f", "the lock file is appended to, not rewritten (holder unreadable)", EX,
     "                os.ftruncate(fh.fileno(), 0)\n",
     "",
     "lock_records_its_holder"),
    ("R2-4i", "the holder note goes through fh's buffer (close() re-raises a failed write after the stops)", EX,
     "                os.ftruncate(fh.fileno(), 0)\n                if os.write(fh.fileno(), note) != len(note):\n"
     '                    log.warning("Lock-holder note written short; a blocked run will report the holder as unknown")\n',
     "                fh.seek(0)\n                fh.truncate()\n                fh.write(note.decode())\n                fh.flush()\n",
     "failed_holder_note"),
    ("R2-4l", "the holder note goes through a second buffered handle closed after the lock (raises at a real full disk's close, after the stops)", MULTI,
     [(EX, "                if os.write(fh.fileno(), note) != len(note):\n"
           '                    log.warning("Lock-holder note written short; a blocked run will report the holder as unknown")\n',
           '                _g = os.fdopen(os.dup(fh.fileno()), "wb")\n                _g.write(note)\n                _g.flush()\n'),
      (EX, "        finally:\n            fcntl.flock(fh, fcntl.LOCK_UN)\n",
           "        finally:\n            fcntl.flock(fh, fcntl.LOCK_UN)\n            _g.close()\n")],
     None,
     "failed_holder_note"),
    ("R2-4m", "the yield sits inside the courtesy try (a body error is logged as a note failure and replaced)", EX,
     '                    log.warning("Lock-holder note written short; a blocked run will report the holder as unknown")\n'
     "            except OSError as e:\n"
     '                log.warning("Could not write the lock-holder note (%s); continuing", e)\n'
     "            yield True\n",
     '                    log.warning("Lock-holder note written short; a blocked run will report the holder as unknown")\n'
     "                yield True\n"
     "            except OSError as e:\n"
     '                log.warning("Could not write the lock-holder note (%s); continuing", e)\n'
     "                yield True\n",
     "error_from_the_body"),
    ("R2-4n", "the lock does not create the state dir (a fresh clone raises before the stops)", EX,
     '    STATE_DIR.mkdir(exist_ok=True)\n    with open(LOCK_FILE, "a+") as fh:\n',
     '    with open(LOCK_FILE, "a+") as fh:\n',
     "creates_its_state_dir"),
    ("R2-4o", "a stray fh.truncate() above the courtesy try (EIO through the file object stops the run)", EX,
     "            try:   # the holder note is a courtesy; on a full disk the lock still holds\n",
     "            fh.truncate(0)\n"
     "            try:   # the holder note is a courtesy; on a full disk the lock still holds\n",
     "failed_truncate"),
    ("R3-5f", "manage reads a failed re-read as an empty book (silent, empty snapshot)", EX,
     "        if not positions:\n"
     "            positions = list(_read(client.get_all_positions))   # one empty response is not evidence\n",
     "        if not positions:\n"
     "            try:\n"
     "                positions = list(_read(client.get_all_positions))   # one empty response is not evidence\n"
     "            except Exception:\n"
     "                positions = []\n",
     "reread_failure"),
    ("R2-4j", "the truncate runs outside the courtesy try (EIO stops the run before the stops)", EX,
     "            try:   # the holder note is a courtesy; on a full disk the lock still holds\n",
     "            os.ftruncate(fh.fileno(), 0)\n"
     "            try:   # the holder note is a courtesy; on a full disk the lock still holds\n",
     "failed_truncate"),
    ("R2-4k", "a short holder note passes silently", EX,
     "                if os.write(fh.fileno(), note) != len(note):\n",
     "                if os.write(fh.fileno(), note) and False:\n",
     "short_holder_note"),
    ("R2-4g", "correlation data client (inside the lock) has no timeout", RE,
     "    client = with_timeout(StockHistoricalDataClient(key, secret))",
     "    client = StockHistoricalDataClient(key, secret)",
     "correlation_data_client"),
    ("R2-4h", "pipeline data client has no timeout", PL,
     "    client = with_timeout(StockHistoricalDataClient(ALPACA_KEY, ALPACA_SECRET))",
     "    client = StockHistoricalDataClient(ALPACA_KEY, ALPACA_SECRET)",
     "pipeline_data_client"),

    # ---- R2-5: staleness bound / replaced orders ------------------------------------------------
    ("R2-5a", "no staleness alert for a pending buy", EX,
     '            if _age_h(pb.get("since")) > PENDING_STALE_H:',
     "            if False:",
     "pending_buy_older_than_18h"),
    ("R2-5b", "no staleness alert for a pending exit", EX,
     '            elif _age_h(pe.get("since")) > PENDING_STALE_H:',
     "            elif False:",
     "pending_exit_older_than_18h or replaced_exit_without"),
    ("R2-5c", "a day-old unreadable pending exit is never cleared (stop frozen)", EX,
     '                if out.state == "unknown" and sym in held:',
     "                if False:",
     "unreadable_pending_exit_older_than_18h"),
    ("R2-5d", "a day-old WORKING pending exit is cleared too (second sell)", EX,
     '                if out.state == "unknown" and sym in held:',
     "                if sym in held:",
     "pending_exit_older_than_18h"),
    ("R2-5e", "a replaced exit is not followed to its replacement", EX,
     '                pe["order_id"] = out.replaced_by\n',
     "                pass\n",
     "replaced_exit_order_is_followed"),
    ("R2-5f", "a replaced pending buy is not followed to its replacement", EX,
     '                pb["order_id"] = out.replaced_by\n',
     "                pass\n",
     "replaced_pending_buy_is_followed"),
    ("R2-5g", "'replaced' status not recognised", EX,
     '            if st == "replaced":\n',
     "            if False:\n",
     "replaced"),
    ("R2-5h", "replacement id read from the wrong field", EX,
     'str(getattr(o, "replaced_by", "") or "")',
     'str(getattr(o, "replaces", "") or "")',
     "replaced"),
    ("R2-5i", "staleness bound far too loose (48h)", EX,
     "PENDING_STALE_H = 18",
     "PENDING_STALE_H = 48",
     "older_than_18h"),

    # ---- R2-6: UnknownOutcome buy recorded as pending -------------------------------------------
    ("R2-6a", "an UNKNOWN buy is not recorded anywhere", EX,
     '            _record_buy(ticker, thesis_entry, sizing["notional"], thesis_entry * (1 - stop_pct),\n'
     '                        target or thesis_entry * (1 + RISK["target_pct"]), th, paper,\n'
     '                        order_id=None, coid=coid, status="unknown", fill_px=None, qty=0.0,\n'
     '                        pending=True, score=score, s=s, sizing=sizing, run_id=scan.get("run_id"))\n',
     "",
     "r2_6"),
    ("R2-6b", "an UNKNOWN buy is recorded as a filled position, not pending", EX,
     "pending=True, score=score",
     "pending=False, score=score",
     "r2_6"),
    ("R2-6c", "manage never looks an UNKNOWN buy up by client_order_id", EX,
     '            if not pb.get("order_id"):\n',
     "            if False:\n",
     "manage_resolves_an_unknown_buy or never_saw_is_dropped or appears_after_the_404"),
    ("R2-6d", "a client_order_id 404 never drops the never-placed buy", EX,
     '                    if _is_not_found(e):\n                        _void_buy(sym, pb, "never placed", paper)',
     '                    if False:\n                        _void_buy(sym, pb, "never placed", paper)',
     "never_saw_is_dropped or r3_17_unknown_buy_never_placed"),
    ("R2-6e", "ANY client_order_id lookup error drops the pending buy", EX,
     '                    if _is_not_found(e):\n                        _void_buy(sym, pb, "never placed", paper)',
     '                    if True:\n                        _void_buy(sym, pb, "never placed", paper)',
     "r2_5_unresolvable or r3_13 or r3_19_non_404_lookup"),
    ("R2-6f", "the looked-up order id is not stored (never resolves)", EX,
     '                    pb["order_id"] = str(o.id)\n',
     "                    pass\n",
     "manage_resolves_an_unknown_buy or appears_after_the_404"),

    # ---- R2-7: transient submit failure + lookup 404 ----------------------------------------------
    ("R2-7a", "a lookup 404 after a timeout/5xx means 'never placed' (round-1 code)", EX,
     "if _is_not_found(lookup_err) and not _is_transient(submit_err):",
     "if _is_not_found(lookup_err):",
     "r2_7"),
    ("R2-7b", "5xx not treated as transient", EX,
     "        return code == 429 or code >= 500",
     "        return code == 429",
     "r2_7"),
    ("R2-7c", "timeouts/dropped connections not treated as transient", EX,
     "    if isinstance(e, (requests.exceptions.RequestException, OSError, ValueError)):\n        return True",
     "    if False:\n        return True",
     "r2_7 or r2_6"),
    ("R2-7d", "an UNKNOWN buy is recorded with the default 8% stop, not the thesis stop", EX,
     'thesis_entry * (1 - stop_pct),\n                        target or',
     'thesis_entry * (1 - RISK["stop_loss_pct"]),\n                        target or',
     "appears_after_the_404"),

    # ---- R2-8: failures after evaluate_exits abort manage -----------------------------------------
    ("R2-8a", "_save re-raises a save failure", EX,
     '"Exits continue; state may be stale."],\n              level="critical", dedupe=False)\n        return False',
     '"Exits continue; state may be stale."],\n              level="critical", dedupe=False)\n        raise',
     "r2_8"),
    ("R2-8b", "the save after evaluate_exits is unguarded", EX,
     "    actions = evaluate_exits(positions, state)\n    _save(state)",
     "    actions = evaluate_exits(positions, state)\n    save_positions_state(state)",
     "state_save_failure_after_evaluate or disk_full"),
    ("R2-8c", "a circuit-breaker failure aborts manage", EX,
     "        try:\n            check_circuit_breaker(account)  # trips halt for today if breached\n"
     "        except Exception as e:\n"
     '            log.error("Circuit breaker check failed (exits continue): %s", e)\n'
     '            alert("Sovereign: circuit breaker check FAILED", [str(e)[:300], "Exits continue."],\n'
     '                  level="critical", dedupe=False)\n',
     "        check_circuit_breaker(account)\n",
     "disk_full_while_the_breaker_trips"),
    ("R2-8d", "a state-save failure is not alerted", EX,
     '        alert("Sovereign: state save FAILED", [str(e)[:300], "Exits continue; state may be stale."],\n'
     '              level="critical", dedupe=False)\n',
     "",
     "state_save_failure_after_evaluate"),
    ("R2-8e", "_finish_exit's save is unguarded (disk full stops the next exit)", EX,
     "    _log_exit(sym, why, order_id, fill_px, qty, st, paper, source)\n    state.pop(sym, None)\n    _save(state)",
     "    _log_exit(sym, why, order_id, fill_px, qty, st, paper, source)\n    state.pop(sym, None)\n"
     "    save_positions_state(state)",
     "disk_full_for_every_state_save or disk_full_exit_is_reported_as_an_exit"),

    # ---- R2-9: torn trade-log line -----------------------------------------------------------------
    ("R2-9a", "a torn tail is not repaired before the next append", EX,
     '                if r.read(1) != b"\\n":\n                    f.write(b"\\n")',
     '                if False:\n                    f.write(b"\\n")',
     "r2_9"),
    ("R2-9b", "one unreadable log line hides every later exit from the dedupe", EX,
     '                except ValueError:\n                    continue\n                if rec.get("action") == "sell"',
     '                except ValueError:\n                    break\n                if rec.get("action") == "sell"',
     "r2_9 or crash_after_logging or log_exit_refuses or pending_exit_logged_once or torn_line_mid_log"),
    ("R2-9c", "the repair always adds a newline (blank lines)", EX,
     '                if r.read(1) != b"\\n":',
     "                if True:",
     "adds_no_blank_line"),

    # ---- R2-10: execute reads state before any order ------------------------------------------------
    ("R2-10a", "execute carries on with an empty book after StateCorrupt", EX,
     '        alert("Sovereign state file CORRUPT — no entries", [str(e)], level="critical")\n        return',
     '        alert("Sovereign state file CORRUPT — no entries", [str(e)], level="critical")\n        state = {}',
     "r2_10 or execute_with_a_corrupt_state_file"),
    ("R2-10b", "a state write failure after a buy escapes (no alert naming the order)", EX,
     "    except Exception as e:\n"
     '        log.error("Bought %s (order %s) but could not record state: %s", ticker, order_id or coid, e)\n',
     "    except ImportError as e:\n"
     '        log.error("Bought %s (order %s) but could not record state: %s", ticker, order_id or coid, e)\n',
     "r2_10_state_write_failure"),
    ("R2-10c", "the state-failure alert does not name the order", EX,
     '              [f"order {order_id or coid}", str(e)[:300], "manage will adopt it',
     '              [str(e)[:300], "manage will adopt it',
     "r2_10_state_write_failure"),
    ("R2-10d", "a corrupt state file in execute is not alerted", EX,
     '        alert("Sovereign state file CORRUPT — no entries", [str(e)], level="critical")\n',
     "",
     "r2_10_corrupt_state"),

    # ---- R2-11: positions outage / two empty lists ---------------------------------------------------
    ("R2-11a", "a positions-read outage is not alerted", EX,
     '        alert("Sovereign: positions unreadable — stops not enforced", [str(e)[:300]], level="critical",\n'
     '              dedupe=False)\n',
     "",
     "positions_outage_alerts"),
    ("R2-11b", "a position the per-symbol check finds is not evaluated", EX,
     "                positions.append(pos)             # the list was wrong; evaluate what the broker returned\n",
     "",
     "two_empty_lists or positions_list_empty_but_broker_confirms or gap_F3b"),

    # ---- R2-12: null current_price ----------------------------------------------------------------------
    ("R2-12a", "one bad position aborts evaluate_exits for all", RE,
     "        except Exception as e:\n            # One bad position",
     "        except ImportError as e:\n            # One bad position",
     "r2_12"),
    ("R2-12b", "a null price reads as 0 (stop fires blind)", RE,
     '    if p.current_price is None:\n        raise ValueError("broker returned no current price")\n'
     "    price = float(p.current_price)",
     "    price = float(p.current_price or 0)",
     "r2_12"),
    ("R2-12c", "unevaluated positions are not alerted", EX,
     '    if bad:\n        alert("Sovereign: stops NOT evaluated"',
     '    if False:\n        alert("Sovereign: stops NOT evaluated"',
     "r2_12"),
    ("R2-12d", "a null price falls back to the entry (untracked adopted blind)", RE,
     '    if p.current_price is None:\n        raise ValueError("broker returned no current price")\n'
     "    price = float(p.current_price)",
     "    price = float(p.current_price if p.current_price is not None else p.avg_entry_price)",
     "r2_12"),
    ("R2-12e", "the snapshot crashes on a null price", EX,
     '            "current": float(p.current_price) if p.current_price is not None else None,',
     '            "current": float(p.current_price),',
     "r2_12"),

    # ---- R2-13: live_price freshness -----------------------------------------------------------------------
    ("R2-13a", "live_price takes any latest trade (no freshness check)", PL,
     " and _fresh_regular_session(t[ticker].timestamp):",
     ":",
     "r2_13 or live_price"),
    ("R2-13b", "a trade exactly at the 09:30 open is refused (boundary)", PL,
     "    return open_today <= ts <= now",
     "    return open_today < ts <= now",
     "trade_freshness_boundaries"),
    ("R2-13c", "a trade exactly 15 minutes old is refused (boundary)", PL,
     "(now - ts) <= timedelta(minutes=max_age_min)",
     "(now - ts) < timedelta(minutes=max_age_min)",
     "trade_freshness_boundaries"),
    ("R2-13d", "pre-market prints count as the session (open at 04:00)", PL,
     "    open_today = now.replace(hour=9, minute=30, second=0, microsecond=0)",
     "    open_today = now.replace(hour=4, minute=0, second=0, microsecond=0)",
     "r2_13"),
    ("R2-13e", "freshness window far too loose (60 min)", PL,
     "max_age_min: float = 15.0",
     "max_age_min: float = 60.0",
     "r2_13"),

    # ---- R2-14: latest_filing over every leg -------------------------------------------------------------------
    ("R2-14a", "latest_filing from each member's first leg only", CS,
     "        for tx in txs:\n            if tx.member.lower().strip() not in members_in:",
     "        for tx in member_txs:\n            if tx.member.lower().strip() not in members_in:",
     "r2_14 or detect_herds"),
    ("R2-14b", "an opposite-direction leg refreshes the herd's filing clock", CS,
     "        for tx in txs:\n            if tx.member.lower().strip() not in members_in:",
     "        for tx in [t for t in transactions if t.ticker == ticker]:\n"
     "            if tx.member.lower().strip() not in members_in:",
     "r2_14"),

    # ---- R2-15: bar request end ------------------------------------------------------------------------------------
    ("R2-15a", "bar request ends at 00:00Z yesterday (drops yesterday's bar)", PL,
     '            start=start_dt.strftime("%Y-%m-%d"),\n',
     '            start=start_dt.strftime("%Y-%m-%d"),\n'
     '            end=(datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d"),\n',
     "r2_15"),
    ("R2-15b", "today's forming bar is kept", PL,
     '.date() < today_et]',
     '.date() <= today_et]',
     "r2_15"),
    ("R2-15c", "the last bar is dropped whatever its date (loses the previous session pre-open and on weekends)", PL,
     "            bar_list = [b for b in bars[ticker]\n"
     "                        if b.timestamp.astimezone(ZoneInfo(\"America/New_York\")).date() < today_et]",
     "            bar_list = list(bars[ticker])[:-1]",
     "r2_15"),
    ("R2-15e", "the last bar is dropped on weekdays from 09:30 ET (loses the previous session on a market holiday)", PL,
     "            bar_list = [b for b in bars[ticker]\n                        if b.timestamp.astimezone(ZoneInfo(\"America/New_York\")).date() < today_et]",
     "            _n = datetime.now(ZoneInfo(\"America/New_York\"))\n"
     "            bar_list = list(bars[ticker])[:-1] if (_n.weekday() < 5 and _n.hour * 60 + _n.minute >= 570) else list(bars[ticker])",
     "r2_15"),
    ("R2-15f", "drop the last bar while open, date filter when closed (open before today's first bar loses a session)", PL,
     "            bar_list = [b for b in bars[ticker]\n                        if b.timestamp.astimezone(ZoneInfo(\"America/New_York\")).date() < today_et]",
     "            bar_list = list(bars[ticker])[:-1] if market_open else [b for b in bars[ticker]\n"
     "                        if b.timestamp.astimezone(ZoneInfo(\"America/New_York\")).date() < today_et]",
     "r2_15"),
    ("R2-15g", "'today' is the UTC date (an evening scan keeps New York's finished-day bar as if it were yesterday's)", PL,
     '            today_et = datetime.now(ZoneInfo("America/New_York")).date()\n',
     "            today_et = datetime.now(timezone.utc).date()\n",
     "r2_15"),
    ("R2-15h", "'today' is the machine's local date (caught on this Pacific box by the 01:00 ET case; blind on a UTC box)", PL,
     '            today_et = datetime.now(ZoneInfo("America/New_York")).date()\n',
     "            today_et = datetime.now().date()\n",
     "r2_15"),
    ("R2-15d", "the last bar is dropped only while the market is open (the close scan keeps today's incomplete bar)", PL,
     "            bar_list = [b for b in bars[ticker]\n"
     "                        if b.timestamp.astimezone(ZoneInfo(\"America/New_York\")).date() < today_et]",
     "            bar_list = list(bars[ticker])[:-1] if market_open else list(bars[ticker])",
     "r2_15"),

    # ---- R2-16: all herds scored ----------------------------------------------------------------------------------
    ("R2-16a", "only the first herd for a ticker is scored", SA,
     '        parts.append(f"{len(s.members)} members {s.direction} (track-weighted {weighted_members:.1f}): {names}{note}")\n',
     '        parts.append(f"{len(s.members)} members {s.direction} (track-weighted {weighted_members:.1f}): {names}{note}")\n'
     "        break\n",
     "r2_16"),
    ("R2-16b", "the summed herd score is not clamped", SA,
     "round(clamp(total), 3)",
     "round(total, 3)",
     "r2_16"),

    # ---- R2-17 / R2-18: backtest ----------------------------------------------------------------------------------
    ("R2-17a", "bt_data reads the pre-adjustment cache", "bt_data.py",
     'CACHE_DIR = STATE_DIR / "bars_cache_adj_all"',
     'CACHE_DIR = STATE_DIR / "bars_cache"',
     "r2_17"),
    ("R2-18a", "backtest docstring claims the live rules again (original line)", "sovereign_backtest.py",
     "  - Exit: -8% stop, +15% target, or 30 trading days, checked on closes. This\n"
     "    is NOT the live exit logic:",
     "  - Exit: -8% stop, +15% target, or 30 trading days — the live rules.\n"
     "    Details:",
     "r2_18"),
    ("R2-18b", "backtest docstring claims the live rules, reworded", "sovereign_backtest.py",
     "  - Exit: -8% stop, +15% target, or 30 trading days, checked on closes. This\n"
     "    is NOT the live exit logic:",
     "  - Exit: -8% stop, +15% target, or 30 trading days (the live rules), checked on closes. This\n"
     "    is the live exit logic:",
     "r2_18"),

    # ================= round-3 findings (a82d324), one group per finding =================

    # ---- R3-1: alert() can never raise ------------------------------------------------------
    ("R3-1a", "a failed dedupe write swallows the alert (not sent)", "sovereign_alerts.py",
     "        except Exception as e:           # cannot record the dedupe: send anyway",
     "        except ZeroDivisionError as e:           # cannot record the dedupe: send anyway",
     "r3_1"),
    ("R3-1b", "a transport failure escapes alert()", "sovereign_alerts.py",
     '    except Exception as e:\n        try:\n            log.error("Alert failed entirely',
     '    except ZeroDivisionError as e:\n        try:\n            log.error("Alert failed entirely',
     "r3_1"),
    ("R3-1c", "the round-2 alert(): its dedupe write raises out of manage on a full disk", "sovereign_alerts.py",
     '    try:\n        key = hashlib.sha256(f"{title}|{level}".encode()).hexdigest()[:16]\n'
     '        try:\n            if dedupe and _already_sent(key):\n'
     '                log.info("Alert deduped: %s", title)\n                return\n'
     '        except Exception as e:           # cannot record the dedupe: send anyway\n'
     '            log.warning("Alert dedupe unavailable (%s); sending", e)\n',
     # the dedupe runs outside every try, as it did before a82d324
     '    key = hashlib.sha256(f"{title}|{level}".encode()).hexdigest()[:16]\n'
     '    if dedupe and _already_sent(key):\n'
     '        log.info("Alert deduped: %s", title)\n        return\n'
     '    try:\n',
     "r3_1 or real_alert"),

    # ---- R3-2: the daily-bar request carries no end ----------------------------------------------
    ("R3-2a", "daily bars requested with end=now (403 on the Basic plan)", PL,
     '            start=start_dt.strftime("%Y-%m-%d"),\n',
     '            start=start_dt.strftime("%Y-%m-%d"),\n            end=datetime.now(timezone.utc),\n',
     "r2_15"),
    ("R3-2b", "daily bars end 14 minutes ago (still inside the SIP window)", PL,
     '            start=start_dt.strftime("%Y-%m-%d"),\n',
     '            start=start_dt.strftime("%Y-%m-%d"),\n'
     '            end=datetime.now(timezone.utc) - timedelta(minutes=14),\n',
     "r2_15"),

    # ---- R3-3: an armed breakeven survives the pending buy resolving -----------------------------
    ("R3-3a", "resolving a pending buy resets an armed breakeven to the 8% rule", EX,
     '    if st.get("trail_armed"):\n        st["stop"] = max(st["stop"], round(st["entry"] * 1.005, 2))\n'
     '    else:\n        st["stop"], _ = enforce_stop_rule(st["entry"], st["stop"])\n    if st.get("target")',
     '    st["stop"], _ = enforce_stop_rule(st["entry"], st["stop"])\n    if st.get("target")',
     "r3_3"),
    ("R3-3b", "resolving keeps an armed breakeven below the real fill", EX,
     '    if st.get("trail_armed"):\n        st["stop"] = max(st["stop"], round(st["entry"] * 1.005, 2))\n    else:',
     '    if st.get("trail_armed"):\n        pass\n    else:',
     "r3_3"),

    # ---- R3-4: wash-trade protection / asynchronous cancel ---------------------------------------
    ("R3-4a", "cancel-before-close is fire-and-forget (the buy is assumed dead at once)", EX,
     '                    bo = _order_outcome(client, pb["order_id"], timeout=CANCEL_WAIT_S)',
     '                    bo = Outcome("dead", None, 0.0, None)',
     "r3_4 or held_shares_of_a_working_buy"),
    ("R3-4b", "a buy not confirmed terminal still gets the close", EX,
     '"{bo.state}; stop stays armed and is retried next run"],\n'
     '                          level="critical", dedupe=False)\n                    continue',
     '"{bo.state}; stop stays armed and is retried next run"],\n'
     '                          level="critical", dedupe=False)',
     "r3_4_buy_that_is_not_confirmed"),
    ("R3-4c", "a late fill during the cancel is not resolved as ours", EX,
     '                if bo.state in ("filled", "partial"):\n                    _resolve_filled_buy(state[ticker], bo)',
     '                if bo.state in ("filled", "partial"):\n                    state[ticker].pop("pending_buy", None)',
     "r3_4_late_fill"),
    ("R3-4d", "the cancel is checked once, never polled", EX,
     '                    bo = _order_outcome(client, pb["order_id"], timeout=CANCEL_WAIT_S)',
     '                    bo = _order_outcome(client, pb["order_id"], timeout=0)',
     "r3_4_manage_waits_for_a_slow_cancel"),
    ("R3-4e", "execute's post-cancel check is not polled", EX,
     "            out = _order_outcome(client, order.id, timeout=CANCEL_WAIT_S)",
     "            out = _order_outcome(client, order.id, timeout=0)",
     "r3_4_execute_waits_for_a_slow_cancel"),
    ("R3-4f", "the stop is silently skipped while the buy cancel is unconfirmed (no alert)", EX,
     '                    alert(f"Exit waiting on a buy cancel: {ticker}",',
     '                    (lambda *a, **k: None)(f"Exit waiting on a buy cancel: {ticker}",',
     "r3_4_buy_that_is_not_confirmed"),
    ("R3-4g", "a dead pending buy keeps blocking the close forever", EX,
     '                elif bo.state in ("dead", "missing"):\n                    del state[ticker]["pending_buy"]',
     '                elif bo.state == "missing":\n                    del state[ticker]["pending_buy"]',
     "r3_4 or held_shares_of_a_working_buy"),

    # ---- R3-5: execute's book includes every tracked entry ---------------------------------------
    ("R3-5a", "only pending buys are added to the book (the round-2 code)", EX,
     '        pb = st.get("pending_buy") or {}\n        if sym not in held:\n',
     '        pb = st.get("pending_buy") or {}\n        if sym not in held and pb:\n',
     "r3_5"),

    # ---- R3-6: a partly filled working buy is priced at its fill ---------------------------------
    ("R3-6a", "a still-working partial buy is recorded at the thesis entry (the round-2 code)", EX,
     "        entry = out.price if (filled or (out.qty > 0 and out.price)) else thesis_entry",
     "        entry = out.price if filled else thesis_entry",
     "r3_6"),
    ("R3-6b", "held shares of a pending buy are not re-anchored to their fill", RE,
     "    else:\n        # Held shares are priced at what was actually paid",
     '    elif not st.get("pending_buy"):\n        # Held shares are priced at what was actually paid',
     "stopped_from_their_actual_fill or below_the_fill_rule or restopped_from_its_broker_fill"),

    # ---- R3-7: one trade exited in pieces is one loss ---------------------------------------------
    ("R3-7a", "partial exit records count toward the loss streak", RE,
     "        if rec.get(\"partial\"):\n            continue",
     "        if False:\n            continue",
     "r3_7"),

    # ---- R3-8: committed-but-unfilled notional is not spendable -----------------------------------
    ("R3-8a", "open broker buy notional is left in spendable cash", EX,
     "        cash -= remainder\n",
     "",
     "r3_8_open_broker_buy"),
    ("R3-8b", "tracked pending buy notional is left in spendable cash", EX,
     '            if pb and str(pb.get("order_id")) not in open_ids:\n                cash -= notional',
     '            if False:\n                cash -= notional',
     "r3_8_tracked"),

    # ---- R3-9: transient errors detected by type --------------------------------------------------
    ("R3-9a", "transient detected by class-name keywords (the round-2 code)", EX,
     "    if isinstance(e, (requests.exceptions.RequestException, OSError, ValueError)):\n        return True",
     '    if any(k in type(e).__name__.lower() for k in ("timeout", "connection", "protocol")):\n        return True',
     "r3_9"),
    ("R3-9b", "an unparseable body is not transient", EX,
     "(requests.exceptions.RequestException, OSError, ValueError)",
     "(requests.exceptions.RequestException, OSError)",
     "r3_9"),
    ("R3-9c", "urllib3 transport errors are not transient", EX,
     "        if isinstance(e, urllib3.exceptions.HTTPError):\n            return True",
     "        if False:\n            return True",
     "r3_9"),
    ("R3-9d", "a requests HTTPError carrying a clean 4xx is transient", EX,
     "        return resp is None or resp.status_code == 429 or resp.status_code >= 500",
     "        return True",
     "r3_9"),

    # ---- R3-10: a trade-log failure in _record_buy ------------------------------------------------
    ("R3-10a", "a buy-log write failure escapes execute", EX,
     '    except Exception as e:\n        log.error("Buy %s (order %s) not written to the trade log',
     '    except ZeroDivisionError as e:\n        log.error("Buy %s (order %s) not written to the trade log',
     "r3_10"),
    ("R3-10b", "a buy-log write failure is not alerted", EX,
     '        alert(f"Buy NOT in trade log: {ticker}", [f"order {order_id or coid}", str(e)[:300]],\n'
     '              level="critical", dedupe=False)\n',
     "",
     "r3_10"),

    # ---- R3-11: Postgres timeouts -----------------------------------------------------------------
    ("R3-11a", "memory DB connection without timeouts", "sovereign_memory.py",
     '        connect_timeout=5, options="-c statement_timeout=5000",\n', "",
     "r3_11"),
    ("R3-11b", "knowledge-graph DB connection without timeouts", "sovereign_kg.py",
     '        connect_timeout=5, options="-c statement_timeout=5000",\n', "",
     "r3_11"),
    ("R3-11c", "memory DB statement timeout far too long (5 min)", "sovereign_memory.py",
     "statement_timeout=5000", "statement_timeout=300000",
     "r3_11"),

    # ---- R3-12: ticker-less critical manage alerts are never deduped ------------------------------
    ("R3-12a", "'positions unreadable' deduped per day", EX,
     '"Sovereign: positions unreadable — stops not enforced", [str(e)[:300]], level="critical",\n'
     '              dedupe=False)',
     '"Sovereign: positions unreadable — stops not enforced", [str(e)[:300]], level="critical")',
     "r3_12"),
    ("R3-12b", "'stops NOT evaluated' deduped per day", EX,
     '    if bad:\n        alert("Sovereign: stops NOT evaluated", [f"{a[\'ticker\']}: {a[\'why\']}" for a in bad], '
     'level="critical",\n              dedupe=False)',
     '    if bad:\n        alert("Sovereign: stops NOT evaluated", [f"{a[\'ticker\']}: {a[\'why\']}" for a in bad], '
     'level="critical")',
     "r3_12"),
    ("R3-12c", "'state save FAILED' deduped per day", EX,
     '"Exits continue; state may be stale."],\n              level="critical", dedupe=False)',
     '"Exits continue; state may be stale."],\n              level="critical")',
     "r3_12"),
    ("R3-12d", "'account unreadable' deduped per day", EX,
     '"Stops still enforced."],\n              level="critical", dedupe=False)',
     '"Stops still enforced."],\n              level="critical")',
     "r3_12"),
    ("R3-12e", "manage's CORRUPT alert deduped per day", EX,
     '"Sovereign state file CORRUPT — stops not enforced", [str(e)], level="critical", dedupe=False)',
     '"Sovereign state file CORRUPT — stops not enforced", [str(e)], level="critical")',
     "r3_12"),
    ("R3-12f", "'lock held' deduped per day", EX,
     'manage skipped."],\n              level="critical", dedupe=False)',
     'manage skipped."],\n              level="critical")',
     "r3_12"),
    ("R3-12g", "'clock unreadable with exits pending' deduped per day", EX,
     "              level=\"critical\", dedupe=False)\n    if not is_open:",
     "              level=\"critical\")\n    if not is_open:",
     "r3_12"),
    ("R3-12h", "'circuit breaker check FAILED' deduped per day", EX,
     '"Exits continue."],\n                  level="critical", dedupe=False)',
     '"Exits continue."],\n                  level="critical")',
     "r3_12 or r3_14"),

    # ---- R3-13: an unresolvable unknown buy reaches the staleness alert ---------------------------
    ("R3-13a", "the staleness alert needs a readable order (the round-2 order of checks)", EX,
     '            if _age_h(pb.get("since")) > PENDING_STALE_H:',
     '            if pb.get("order_id") and _age_h(pb.get("since")) > PENDING_STALE_H:',
     "unresolvable_unknown or r3_13"),

    # ---- R3-14: breaker and snapshot failures are alerted ------------------------------------------
    ("R3-14a", "a circuit-breaker failure is only logged", EX,
     '            alert("Sovereign: circuit breaker check FAILED", [str(e)[:300], "Exits continue."],\n'
     '                  level="critical", dedupe=False)\n',
     "",
     "r3_14 or breaker_failure_is_alerted"),
    ("R3-14b", "a snapshot failure is only logged", EX,
     '            alert("Sovereign: account snapshot failed", [str(e)[:300], "Exits continue."],\n'
     '                  level="warning", dedupe=False)\n',
     "",
     "r3_14 or snapshot_failure_is_alerted"),

    # ---- R3-15: a 404 on an order is 'missing' ------------------------------------------------------
    ("R3-15a", "a 404 on an order reads as 'unknown' (the round-2 _order_outcome)", EX,
     '            if _is_not_found(e):\n                return Outcome("missing", None, 0.0, None)',
     '            if False:\n                return Outcome("missing", None, 0.0, None)',
     "r3_15"),
    ("R3-15b", "a pending exit whose order is missing is kept", EX,
     '            elif out.state == "missing":\n                del st["pending_exit"]',
     '            elif out.state == "missing_x":\n                del st["pending_exit"]',
     "r3_15"),
    ("R3-15c", "a pending buy whose order is missing is kept", EX,
     '            elif out.state in ("dead", "missing"):\n                _void_buy',
     '            elif out.state == "dead":\n                _void_buy',
     "r3_15"),
    ("R3-15d", "a missing exit order is not alerted", EX,
     '                alert(f"Exit order not found: {sym}"',
     '                (lambda *a, **k: None)(f"Exit order not found: {sym}"',
     "r3_15"),

    # ---- R3-16: a malformed state entry is a corrupt file ---------------------------------------------
    ("R3-16a", "malformed state entries are accepted", RE,
     '    if bad:\n        raise StateCorrupt(f"{POSITIONS_STATE}: malformed entries {bad}")',
     '    if False:\n        raise StateCorrupt(f"{POSITIONS_STATE}: malformed entries {bad}")',
     "r3_16"),
    ("R3-16b", "only non-dict entries are malformed (missing stop/entry accepted)", RE,
     'not isinstance(v, dict) or "stop" not in v or "entry" not in v]',
     "not isinstance(v, dict)]",
     "r3_16"),

    # ---- R3-17: a buy that never became a position is voided in the log -------------------------------
    ("R3-17a", "a never-placed buy leaves its buy line unanswered", EX,
     '                        _void_buy(sym, pb, "never placed", paper)\n', "",
     "r3_17"),
    ("R3-17b", "a dead/missing pending buy leaves its buy line unanswered", EX,
     '                _void_buy(sym, pb, f"order {out.state} with nothing filled", paper)\n', "",
     "r3_17 or r3_15_pending_buy"),
    ("R3-17c", "_void_buy writes nothing", EX,
     '        _log_trade({"action": "buy_void", "ticker": sym,',
     '        (lambda r: None)({"action": "buy_void", "ticker": sym,',
     "r3_17 or r3_15_pending_buy"),

    # ---- R3-18: a re-anchor that lowers a stop is alerted; breakeven re-armed ----------------------------
    ("R3-18a", "reconcile_entry never marks a lowered stop", RE,
     '    if st["stop"] < old_stop:\n        st["_stop_lowered"]',
     '    if False:\n        st["_stop_lowered"]',
     "r3_18"),
    ("R3-18b", "manage never alerts a lowered stop", EX,
     "        if lowered:\n            alert(f\"Stop lowered on re-anchor: {sym}\",",
     "        if False:\n            alert(f\"Stop lowered on re-anchor: {sym}\",",
     "r3_18"),
    ("R3-18c", "the transient lowered-stop marker is persisted into state", EX,
     '        lowered = st.pop("_stop_lowered", None)',
     '        lowered = st.get("_stop_lowered")',
     "r3_18"),
    ("R3-18d", "an armed breakeven is left below the real fill (the pre-a82d324 reconcile)", RE,
     '        st["stop"] = max(st["stop"], round(st["entry"] * 1.005, 2))\n    else:\n'
     '        st["stop"], _ = enforce_stop_rule(st["entry"], st["stop"])\n    if st["stop"] < old_stop:',
     '        pass\n    else:\n'
     '        st["stop"], _ = enforce_stop_rule(st["entry"], st["stop"])\n    if st["stop"] < old_stop:',
     "reconcile_entry_rearms or armed_breakeven_is_rearmed"),

    # ---- R3-19: test gaps (the missed round-2 mutants are re-pointed in place above) --------------------
    ("R3-19a", "the buy notional is sent unrounded (422 at the broker)", EX,
     "symbol=ticker, notional=round(notional, 2), client_order_id=client_order_id,",
     "symbol=ticker, notional=notional, client_order_id=client_order_id,",
     "r3_19_buy_is_a_day_market_order"),
    ("R3-19b", "buys sent OPG (fractional/notional orders must be DAY)", EX,
     "            side=OrderSide.BUY, time_in_force=TimeInForce.DAY))",
     "            side=OrderSide.BUY, time_in_force=TimeInForce.OPG))",
     "r3_19_buy_is_a_day_market_order or entry_is_the_fill"),

    # ================= round-4 additions: one more real bug per round-3 finding =================
    # Each re-creates a different way the same finding could come back. At a82d324
    # + the round-3 suite, seven survive the WHOLE suite (not just their -k): R3-2d,
    # R3-4i, R3-5b, R3-5c, R3-6c, R3-8c, R:log-before-delete-unresolved. They are
    # test gaps, left in so the runner stays red until tests cover them.
    ("R3-1d", "alert() ignores dedupe=False (every critical alert deduped per day)", "sovereign_alerts.py",
     "            if dedupe and _already_sent(key):",
     "            if _already_sent(key):",
     "r3_12 or r3_1"),
    ("R3-1e", "a dedupe-file failure drops the alert instead of sending it", "sovereign_alerts.py",
     '            log.warning("Alert dedupe unavailable (%s); sending", e)\n',
     '            log.warning("Alert dedupe unavailable (%s); sending", e)\n            return\n',
     "r3_1"),
    ("R3-2c", "daily bars carry end=now whenever the market is open", PL,
     '            start=start_dt.strftime("%Y-%m-%d"),\n',
     '            start=start_dt.strftime("%Y-%m-%d"),\n'
     '            end=datetime.now(timezone.utc) if market_open else None,\n',
     "r2_15"),
    ("R3-2d", "correlation bars (execute's sizing path) request end=now: 403, penalty silently off", RE,
     '            end=(datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")))',
     "            end=datetime.utcnow()))",
     "bar_requests or r2_4_correlation or r2_15 or correlation"),
    ("R3-3c", "resolving a pending buy takes the LOWER of the armed breakeven and fill+0.5%", EX,
     '        st["stop"] = max(st["stop"], round(st["entry"] * 1.005, 2))\n    else:\n'
     '        st["stop"], _ = enforce_stop_rule(st["entry"], st["stop"])\n    if st.get("target")',
     '        st["stop"] = min(st["stop"], round(st["entry"] * 1.005, 2))\n    else:\n'
     '        st["stop"], _ = enforce_stop_rule(st["entry"], st["stop"])\n    if st.get("target")',
     "r3_3 or pending_buy_filled_later"),
    ("R3-4h", "a refused/failed cancel of the pending buy aborts the exit (no close, no re-check)", EX,
     '                        log.warning("Cancel of pending buy %s before the exit failed: %s", ticker, e)\n',
     '                        log.warning("Cancel of pending buy %s before the exit failed: %s", ticker, e)\n'
     '                        raise\n',
     "r3_4 or held_shares_of_a_working_buy"),
    ("R3-4i", "a pending buy known only by client_order_id is assumed dead and the close goes out", EX,
     '                    bo = Outcome("unknown", None, 0.0, None)',
     '                    bo = Outcome("dead", None, 0.0, None)',
     "r3_4 or unknown_buy or held_shares_of_a_working_buy or known_only_by_client_id"),
    ("R3-4j", "execute: a failed cancel of an unfilled buy escapes (buy never recorded)", EX,
     '                log.warning("Cancel of unfilled buy %s (%s) failed: %s", ticker, order.id, e)\n',
     '                log.warning("Cancel of unfilled buy %s (%s) failed: %s", ticker, order.id, e)\n'
     '                raise\n',
     "r3_4 or unfilled_buy or partially_filled_buy or buy_whose_cancel_fails"),
    ("R3-4k", "execute ignores the post-cancel outcome (records the pre-cancel 'open')", EX,
     "            out = _order_outcome(client, order.id, timeout=CANCEL_WAIT_S)",
     "            _order_outcome(client, order.id, timeout=CANCEL_WAIT_S)",
     "r3_4 or unfilled_buy or partially_filled_buy"),
    ("R3-5b", "execute does not re-read an empty positions list", EX,
     "    if not positions:\n"
     "        positions = list(_read(client.get_all_positions))   # one empty response is not evidence\n"
     "    open_buys = _open_buys(client)",
     "    open_buys = _open_buys(client)",
     "r3_5 or empty_positions or one_empty"),
    ("R3-5c", "a tracked entry with a pending exit is left out of execute's book", EX,
     '        pb = st.get("pending_buy") or {}\n        if sym not in held:\n',
     '        pb = st.get("pending_buy") or {}\n        if sym not in held and not st.get("pending_exit"):\n',
     "r3_5 or pending_exit_counts_in_the_book"),
    ("R3-6c", "a still-working partial buy is recorded at its filled notional (commitment understated)", EX,
     '        notional = round(out.price * out.qty, 2) if filled else sizing["notional"]',
     '        notional = round(out.price * out.qty, 2) if (filled or out.qty > 0) else sizing["notional"]',
     "r3_6 or partial_buy or r3_8"),
    ("R3-7b", "a partial exit record breaks the loss streak instead of being skipped", RE,
     "        if rec.get(\"partial\"):\n            continue",
     "        if rec.get(\"partial\"):\n            break",
     "r3_7"),
    ("R3-8c", "in-run buys are not taken out of spendable cash", EX,
     "        tracked.add(ticker)\n        cash -= notional\n",
     "        tracked.add(ticker)\n",
     "r3_8 or buys_in_one_run or cash"),
    ("R3-8d", "an open notional buy is valued as qty x limit (0 for our market buys)", EX,
     "        full = _committed(o)\n",
     "        full = float(o.qty or 0) * float(o.limit_price or 0)\n",
     "r3_8_open_broker_buy or open_buy_order"),
    ("M-1a", "an open buy of unknown cost commits $0 (the round-4 code): all cash reads as spendable", EX,
     "    return cost if cost and cost > 0 else None\n",
     "    return cost if cost and cost > 0 else 0.0\n",
     "unknown_cost or priced_qty_open_buy"),
    ("M-1b", "a stop buy is priced at its trigger, the least it can cost (a gap overspends the reserve)", EX,
     "    px = float(o.limit_price or 0)\n",
     '    px = float(o.limit_price or 0) or float(getattr(o, "stop_price", None) or 0)\n',
     "unknown_cost"),
    ("M-1c", "a stop-limit buy is priced at its stop, not its limit (the most it can pay)", EX,
     "    px = float(o.limit_price or 0)\n",
     '    px = float(getattr(o, "stop_price", None) or 0) or float(o.limit_price or 0)\n',
     "priced_qty_open_buy"),
    ("M-1d", "a priced qty open buy counts $0 toward its sector (cash-side only)", EX,
     "positions.append(SimpleNamespace(symbol=o.symbol, market_value=full))",
     "positions.append(SimpleNamespace(symbol=o.symbol, market_value=float(o.notional or 0)))",
     "priced_qty_open_buy_counts_toward_its_sector"),
    ("R3-8e", "an open buy's commitment is deducted only when its symbol is not already held (the round-3 code)", EX,
     "        cash -= remainder\n        if o.symbol not in held:\n",
     "        if o.symbol not in held:\n            cash -= remainder\n",
     "r3_8_unfilled_remainder"),
    ("R3-5d", "execute re-reads an empty positions list only when something is tracked (pre-2026-09-26)", EX,
     "    if not positions:\n        positions = list(_read(client.get_all_positions))",
     "    if not positions and state:\n        positions = list(_read(client.get_all_positions))",
     "empty_positions_list_with_nothing_tracked"),
    ("M-1e", "an option buy is priced as qty x limit, 1/100 of what a contract commits", EX,
     '    if getattr(ac, "value", ac) not in ("us_equity", "crypto"):\n',
     '    if getattr(ac, "value", ac) == "no_such_class":\n',
     "option_buy or committed_prices_only"),
    ("M-1f", "an open buy on a symbol already on the book does not count toward its sector", EX,
     "            extra[o.symbol] = extra.get(o.symbol, 0.0) + remainder\n",
     "            pass\n",
     "held_symbol_counts_toward_its_sector"),
    ("M-1j", "the fold adds a held symbol's full commitment, double-counting what already filled", EX,
     "            extra[o.symbol] = extra.get(o.symbol, 0.0) + remainder\n",
     "            extra[o.symbol] = extra.get(o.symbol, 0.0) + full\n",
     "adds_only_its_remainder"),
    ("M-1k", "an open buy on a held symbol becomes a second book entry (counts toward max_positions)", EX,
     "            extra[o.symbol] = extra.get(o.symbol, 0.0) + remainder\n",
     "            positions.append(SimpleNamespace(symbol=o.symbol, market_value=remainder))\n",
     "not_a_second_position"),
    ("M-1g", "an open buy folds only into a broker-held symbol (a second open buy counts $0)", EX,
     "        else:\n            # Already on the book",
     "        elif o.symbol in {p.symbol for p in positions if not isinstance(p, SimpleNamespace)}:\n            # Already on the book",
     "two_open_buys_on_one_symbol"),
    ("M-1h", "a position with no market value is read as $0 (a same-sector buy oversizes)", MULTI,
     [(EX, "    if unvalued:\n        log.error(", "    if False:\n        log.error("),
      (EX, "market_value=float(p.market_value) + extra[p.symbol])",
           "market_value=float(p.market_value or 0) + extra[p.symbol])")],
     None,
     "position_of_unknown_value"),
    ("M-1l", "the value screen checks only None (a NaN market value switches the sector cap off)", EX,
     '    unvalued = [p.symbol for p in positions if not _usable_value(getattr(p, "market_value", None))]',
     '    unvalued = [p.symbol for p in positions if getattr(p, "market_value", None) is None]',
     "position_of_unknown_value"),
    ("M-1m", "a negative notional is taken at face value", EX,
     "        return notional if notional > 0 else None\n",
     "        return notional\n",
     "committed_prices_only"),
    ("M-1i", "a non-positive cost is taken at face value (a net credit reads as spendable)", EX,
     "    return cost if cost and cost > 0 else None\n",
     "    return cost\n",
     "committed_prices_only"),
    ("R3-5e", "manage re-reads an empty positions list only when something is tracked (pre-2026-09-26)", EX,
     "        if not positions:\n            positions = list(_read(client.get_all_positions))   # one empty response is not evidence\n    except",
     "        if not positions and state:\n            positions = list(_read(client.get_all_positions))   # one empty response is not evidence\n    except",
     "manage_rereads"),
    ("R3-9e", "a 429 is not transient (a rate-limited buy reads as a clean refusal)", EX,
     "        return code == 429 or code >= 500",
     "        return code >= 500",
     "r3_9 or r2_7 or transient"),
    ("R3-10c", "a buy-log failure skips the state write (buy in neither log nor state)", EX,
     '        alert(f"Buy NOT in trade log: {ticker}", [f"order {order_id or coid}", str(e)[:300]],\n'
     '              level="critical", dedupe=False)\n',
     '        alert(f"Buy NOT in trade log: {ticker}", [f"order {order_id or coid}", str(e)[:300]],\n'
     '              level="critical", dedupe=False)\n        return\n',
     "r3_10"),
    ("R3-11d", "knowledge-graph statement timeout far too long (5 min)", "sovereign_kg.py",
     "statement_timeout=5000", "statement_timeout=300000",
     "r3_11"),
    ("R3-11e", "memory DB connect_timeout=0 (libpq: wait forever)", "sovereign_memory.py",
     "connect_timeout=5,", "connect_timeout=0,",
     "r3_11"),
    ("R3-12i", "the alert dedupe never resets at midnight (one outage silences every later day)",
     "sovereign_alerts.py",
     '    if sent.get("date") != today:',
     '    if not sent.get("date"):',
     "r3_12"),
    ("R3-13b", "the pending-buy staleness check runs after the lookup (a failing lookup skips it)", EX,
     '            # Staleness first, so an order that can never be read still alerts.\n'
     '            if _age_h(pb.get("since")) > PENDING_STALE_H:\n'
     '                alert(f"Pending buy STALE: {sym}", [f"order {pb.get(\'order_id\') or pb.get(\'client_order_id\')} "\n'
     '                                                    f"unresolved after {_age_h(pb.get(\'since\')):.0f}h"],\n'
     '                      level="critical")\n'
     '            if not pb.get("order_id"):\n'
     '                try:\n'
     '                    o = _read(client.get_order_by_client_id, pb["client_order_id"])\n'
     '                    pb["order_id"] = str(o.id)\n'
     '                except Exception as e:\n'
     '                    if _is_not_found(e):\n'
     '                        _void_buy(sym, pb, "never placed", paper)\n'
     '                        del state[sym]\n'
     '                        _save(state)\n'
     '                        continue\n'
     '                    raise\n',
     '            if not pb.get("order_id"):\n'
     '                try:\n'
     '                    o = _read(client.get_order_by_client_id, pb["client_order_id"])\n'
     '                    pb["order_id"] = str(o.id)\n'
     '                except Exception as e:\n'
     '                    if _is_not_found(e):\n'
     '                        _void_buy(sym, pb, "never placed", paper)\n'
     '                        del state[sym]\n'
     '                        _save(state)\n'
     '                        continue\n'
     '                    raise\n'
     '            if _age_h(pb.get("since")) > PENDING_STALE_H:\n'
     '                alert(f"Pending buy STALE: {sym}", [f"order {pb.get(\'order_id\') or pb.get(\'client_order_id\')} "\n'
     '                                                    f"unresolved after {_age_h(pb.get(\'since\')):.0f}h"],\n'
     '                      level="critical")\n',
     "r3_13 or unresolvable_unknown or r2_5"),
    ("R3-14c", "a circuit-breaker failure is alerted only as a warning", EX,
     '            alert("Sovereign: circuit breaker check FAILED", [str(e)[:300], "Exits continue."],\n'
     '                  level="critical", dedupe=False)',
     '            alert("Sovereign: circuit breaker check FAILED", [str(e)[:300], "Exits continue."],\n'
     '                  level="warning", dedupe=False)',
     "r3_14"),
    ("R3-14d", "the snapshot-failure alert does not say what failed", EX,
     '            alert("Sovereign: account snapshot failed", [str(e)[:300], "Exits continue."],',
     '            alert("Sovereign: account snapshot failed", ["Exits continue."],',
     "r3_14"),
    ("R3-15e", "a 404'd pending exit re-arms only once the symbol is no longer held", EX,
     '            elif out.state == "missing":\n                del st["pending_exit"]',
     '            elif out.state == "missing" and sym not in held:\n                del st["pending_exit"]',
     "r3_15"),
    ("R3-16c", "a state entry missing its entry price is accepted", RE,
     'not isinstance(v, dict) or "stop" not in v or "entry" not in v]',
     'not isinstance(v, dict) or "stop" not in v]',
     "r3_16"),
    ("R3-17d", "the buy_void record does not name the order it voids", EX,
     '        _log_trade({"action": "buy_void", "ticker": sym, "order_id": pb.get("order_id"),\n'
     '                    "client_order_id": pb.get("client_order_id"), "why": why, "paper": paper})',
     '        _log_trade({"action": "buy_void", "ticker": sym, "why": why, "paper": paper})',
     "r3_17 or r3_15_pending_buy"),
    ("R3-18e", "a re-anchor that keeps the stop unchanged is alerted as 'lowered'", RE,
     '    if st["stop"] < old_stop:\n        st["_stop_lowered"]',
     '    if st["stop"] <= old_stop:\n        st["_stop_lowered"]',
     "r3_18"),
    ("R3-19c", "buys sent IOC (fractional/notional orders must be DAY)", EX,
     "            side=OrderSide.BUY, time_in_force=TimeInForce.DAY))",
     "            side=OrderSide.BUY, time_in_force=TimeInForce.IOC))",
     "r3_19_buy_is_a_day_market_order or entry_is_the_fill"),

    # ---- docstring rules not covered above --------------------------------------------------------
    ("R:log-components", "buy log drops the signal components (composite score, conviction)", EX,
     '                    "composite": score, "conviction": s["conviction"],\n',
     "",
     "buy_line_carries_the_sizing_math"),
    ("R:exit-qty", "an exit record drops the shares it sold", EX,
     '"qty": qty, "partial": partial, "entry": entry or None,',
     '"qty": None, "partial": partial, "entry": entry or None,',
     "partial_exit or log_exit"),
    ("R:log-before-delete-unresolved", "a vanished position's state is deleted before its record is written", EX,
     '            _log_trade({"action": "reconcile_unresolved", "ticker": sym, "paper": paper,\n'
     '                        "why": "broker has no position and no sell since it opened", "state": st})\n'
     '            state.pop(sym, None)\n            _save(state)\n',
     '            state.pop(sym, None)\n            _save(state)\n'
     '            _log_trade({"action": "reconcile_unresolved", "ticker": sym, "paper": paper,\n'
     '                        "why": "broker has no position and no sell since it opened", "state": st})\n',
     "vanished_with_no_sell or disk_full or log_failure or vanished_position_keeps_its_state"),

    # ---------------- live_price ------------------------------------------------------------
    ("LP1", "the ask is used when the spread is wide (the original price)", PL,
     "            if bid > 0 and ask > 0 and (ask - bid) / ((ask + bid) / 2) < MAX_SPREAD_FOR_MID:\n"
     "                return round((ask + bid) / 2, 4)",
     "            if ask > 0:\n                return ask",
     "live_price"),
    ("LP2", "a spread of exactly 1% counts as tight", PL,
     "(ask - bid) / ((ask + bid) / 2) < MAX_SPREAD_FOR_MID:",
     "(ask - bid) / ((ask + bid) / 2) <= MAX_SPREAD_FOR_MID:",
     "live_price_exactly_one_percent"),
    ("LP3", "a zero trade price is accepted", PL,
     "        if ticker in t and float(t[ticker].price) > 0 and _fresh_regular_session(t[ticker].timestamp):",
     "        if ticker in t and _fresh_regular_session(t[ticker].timestamp):",
     "live_price"),
    ("LP4", "the quote is preferred over the latest trade", PL,
     "    try:\n        t = client.get_stock_latest_trade(TradeReq(symbol_or_symbols=[ticker]))",
     "    try:\n        raise RuntimeError('skip trade')\n        t = client.get_stock_latest_trade(TradeReq(symbol_or_symbols=[ticker]))",
     "live_price_prefers_the_latest_trade or get_stock_data_prices_from_the_trade"),
    ("LP5", "get_stock_data still prices from the ask", PL,
     "        data.current_price = live_price(client, ticker, data.current_price,\n"
     "                                        StockLatestTradeRequest, StockLatestQuoteRequest)",
     "        data.current_price = float(client.get_stock_latest_quote(\n"
     "            StockLatestQuoteRequest(symbol_or_symbols=[ticker]))[ticker].ask_price)",
     "get_stock_data_prices_from_the_trade"),
    ("LP6", "the midpoint is computed wrong (ask + bid, no halving)", PL,
     "                return round((ask + bid) / 2, 4)",
     "                return round((ask + bid), 4)",
     "live_price_uses_the_mid"),

    # ---------------- split/dividend-adjusted bars (one per site) ------------------------------
    ("BA1", "pipeline bars unadjusted", PL,
     "            adjustment=Adjustment.ALL,   # raw bars turned a 4:1 split into a \"crash\"\n", "",
     "split_adjusted or names_an_adjustment or adjustment_all"),
    ("BA2", "risk_engine bars split-only (dividends unadjusted)", RE,
     "_ADJ_ALL = _Adjustment.ALL", "_ADJ_ALL = _Adjustment.SPLIT",
     "split_adjusted or adj_all"),
    ("BA3", "bt_data bars unadjusted", "bt_data.py",
     "StockBarsRequest(adjustment=_ADJ_ALL, ", "StockBarsRequest(",
     "split_adjusted or names_an_adjustment or adjustment_all"),
    ("BA4", "correlation_breaks bars unadjusted", "correlation_breaks.py",
     "StockBarsRequest(adjustment=_ADJ_ALL, ", "StockBarsRequest(",
     "split_adjusted or names_an_adjustment or adjustment_all"),
    ("BA5", "macro_regime sector-dispersion bars unadjusted", "macro_regime.py",
     "StockBarsRequest(adjustment=_ADJ_ALL, \n            symbol_or_symbols=sector_etfs",
     "StockBarsRequest(\n            symbol_or_symbols=sector_etfs",
     "split_adjusted or names_an_adjustment or adjustment_all"),
    ("BA6", "macro_regime SPY trend bars unadjusted", "macro_regime.py",
     "StockBarsRequest(adjustment=_ADJ_ALL, \n            symbol_or_symbols=[\"SPY\"]",
     "StockBarsRequest(\n            symbol_or_symbols=[\"SPY\"]",
     "split_adjusted or names_an_adjustment or adjustment_all"),
    ("BA7", "member_scoring bars unadjusted", "member_scoring.py",
     "StockBarsRequest(adjustment=_ADJ_ALL, ", "StockBarsRequest(",
     "split_adjusted or names_an_adjustment or adjustment_all"),
    ("BA8", "sovereign_backtest bars unadjusted", "sovereign_backtest.py",
     "StockBarsRequest(adjustment=_ADJ_ALL, ", "StockBarsRequest(",
     "split_adjusted or names_an_adjustment or adjustment_all"),
    ("BA9", "sovereign_opportunity bars unadjusted", "sovereign_opportunity.py",
     "StockBarsRequest(adjustment=_ADJ_ALL, ", "StockBarsRequest(",
     "split_adjusted or names_an_adjustment or adjustment_all"),
    # R2-19: the strengthened structural check (aliases, RAW/SPLIT, **kw, module constants)
    ("BA10", "bt_data's _ADJ_ALL bound to RAW", "bt_data.py",
     "_ADJ_ALL = _Adjustment.ALL", "_ADJ_ALL = _Adjustment.RAW",
     "adj_all or adjustment_all or split_adjusted"),
    ("BA11", "pipeline bars split-only (a direct enum member, not the alias)", PL,
     "            adjustment=Adjustment.ALL,   # raw", "            adjustment=Adjustment.SPLIT,   # raw",
     "adjustment_all or split_adjusted or r2_15"),
    ("BA12", "sovereign_backtest's _ADJ_ALL bound to RAW", "sovereign_backtest.py",
     "_ADJ_ALL = _Adjustment.ALL", "_ADJ_ALL = _Adjustment.RAW",
     "adj_all or adjustment_all or split_adjusted"),
    ("BA13", "correlation_breaks passes RAW directly", "correlation_breaks.py",
     "StockBarsRequest(adjustment=_ADJ_ALL, ", "StockBarsRequest(adjustment=_Adjustment.RAW, ",
     "adjustment_all or split_adjusted"),
    ("BA14", "member_scoring hides the adjustment in **kwargs", "member_scoring.py",
     "StockBarsRequest(adjustment=_ADJ_ALL, ", "StockBarsRequest(**{\"adjustment\": \"raw\"}, ",
     "adjustment_all or split_adjusted"),

    # ---------------- congress decay -------------------------------------------------------------
    ("CD1", "decay reads a field HerdSignal never had (the original bug: always 1.0)", SA,
     "        recency = herd_recency_days(s)",
     '        recency = getattr(s, "recency_days", None) or 0',
     "congress_decay or r2_16"),
    ("CD2", "decay clocked from the transaction date, not the filing", CS,
     '                filed.append(datetime.strptime(tx.filing_date, "%m/%d/%Y"))',
     '                filed.append(datetime.strptime(tx.transaction_date, "%m/%d/%Y"))',
     "detect_herds_stamps_the_newest_filing or r2_14"),
    ("CD3", "decay clocked from the OLDEST leg", CS,
     '        latest_filing = max(filed).strftime("%Y-%m-%d") if filed else ""',
     '        latest_filing = min(filed).strftime("%Y-%m-%d") if filed else ""',
     "detect_herds_stamps_the_newest_filing or r2_14"),
    ("CD4", "detect_herds never stamps the filing date", CS,
     "            latest_filing=latest_filing,\n", "",
     "detect_herds_stamps_the_newest_filing"),
    ("CD5", "unknown filing date treated as ancient (fully decayed)", SA,
     "    if not lf:\n        return 0\n", "    if not lf:\n        return 999\n",
     "unknown_filing_date or herd_recency_days"),
    ("CD6", "decay slope wrong (reaches 0 at 90 days)", SA,
     "            decay = (60 - recency) / 30.0", "            decay = (90 - recency) / 60.0",
     "congress_decay_weights_by_filing_age"),
    ("CD7", "decay only applied to buys", SA,
     "        magnitude *= decay\n", "        magnitude *= decay if s.direction == \"buy\" else 1.0\n",
     "congress_decay_applies_to_sells_too or r2_16"),
    ("CD8", "unknown filing date not flagged in the detail", SA,
     'else "" if _filing_known(s) else ", filing date unknown (no decay)")',
     'else "")',
     "unknown_filing_date_means_no_decay"),
    ("CD9", "herd_recency_days ignores dict signals", SA,
     '    return getattr(s, "latest_filing", None) or (s.get("latest_filing") if isinstance(s, dict) else None)',
     '    return getattr(s, "latest_filing", None)',
     "herd_recency_days_reads_objects_and_dicts"),

    # ---------------- kept from the earlier runner --------------------------------------------------
    ("K:stop-rule", "stop rule: no widening", RE,
     "    if stop > floor:\n        return floor,", "    if False:\n        return floor,",
     "tight_llm_stop or llm_stop_tighter"),
    ("K:thesis-raw-stop", "thesis keeps the raw LLM stop", PL,
     "        stop_loss=stop,\n        target=target,", "        stop_loss=_num(data.get('stop_loss')),\n        target=target,",
     "llm_stop_tighter_than_rule"),
    ("K:backend", "backend switch ignored", PL,
     'if backend in ("auto", "sdk"):', 'if True:', "cli_backend_never"),
    ("K:cli-stdout", "CLI stdout not logged", PL,
     "result.stderr[:300], result.stdout[:300])", "result.stderr[:300], '')", "cli_failure_logs_stdout"),
    ("K:snapshot-order", "snapshot saved before the gates", PL,
     "    # Composite signal per ticker — deterministic, explainable\n",
     "    # Composite signal per ticker — deterministic, explainable\n    save_snapshot(signals)  # moved back\n",
     "snapshot_is_saved_after"),
    ("K:pairing", "newest signals file wins (the original pairing bug)", EX,
     'sigs = load_json(RESULTS_DIR / scan["signals_file"], None)',
     'sigs = load_json(sorted(RESULTS_DIR.glob("signals_*.json"))[-1], None)', "paired_with_its_own"),
    ("K:scan-age", "scan age limit back at the original 4h", EX,
     "    if age_h > MAX_SCAN_AGE_H:", "    if age_h > 4:", "stale_scan_is_refused"),
    ("K:empty-scan", "empty/halted scan not refused", EX,
     "    if not scan.get(\"theses\"):", "    if False:", "crisis_scan_blocks"),
    ("K:fill-ignored", "fill price ignored: entry stays the thesis", EX,
     "        entry = out.price if (filled or (out.qty > 0 and out.price)) else thesis_entry",
     "        entry = thesis_entry",
     "entry_is_the_fill or partial_buy_is_a_position"),
    ("K:execute-floor", "execute stop floor removed", EX,
     "        stop_pct = max(stop_pct, RISK[\"stop_loss_pct\"])\n", "", "floors_a_tight_stop"),
    ("K:no-coid", "buys sent with no client_order_id", EX,
     "notional=round(notional, 2), client_order_id=client_order_id,", "notional=round(notional, 2),",
     "client_order_id or raised_but_was_accepted"),
    ("K:buy-recovery", "buy recovery removed: a raised submit is always a failure", EX,
     "    except Exception as submit_err:\n        try:\n",
     "    except Exception as submit_err:\n        raise\n        try:\n",
     "raised_but_was_accepted or submit_recovery_returns_this_attempts"),
    ("K:streak-pnl", "loss streak ignores pnl (STOP label rules)", RE,
     "        if rec.get(\"pnl_pct\") is not None:", "        if False:", "breakeven_stop_is_not_a_loss"),
    ("K:snapshot-key", "snapshot drops a contract key", EX,
     "        \"last_equity\": float(account.last_equity or account.equity),\n", "",
     "snapshot_with_contract_keys"),
]


def _deselect_args():
    out = []
    for t in KNOWN_RED:
        out += ["--deselect", f"{TESTS}::{t}"]
    return out


def run(selector: str, root: pathlib.Path) -> int:
    return subprocess.run([PY, "-m", "pytest", TESTS, "-q", "-x", "-k", selector,
                           "-p", "no:cacheprovider", *_deselect_args()],
                          cwd=root, capture_output=True, text=True).returncode


IGNORE = shutil.ignore_patterns(".git", "__pycache__", "sovereign_results", "sovereign_state",
                                "congress_data", ".claude", ".agent-army", ".envrc", "node_modules",
                                "legacy")


def _edits(m):
    """A mutation's edits as (file, old, new) triples. `file` may be MULTI, in
    which case `old` is the list of triples: one bug that takes two edits (a
    guard and its backstop) to put back."""
    _, _, fname, old, new, _ = m
    return list(old) if fname == MULTI else [(fname, old, new)]


def check(m) -> tuple:
    mid, name, _, _, _, sel = m
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td) / "repo"
        shutil.copytree(REPO, root, ignore=IGNORE)
        for fname, old, new in _edits(m):
            f = root / fname
            src = f.read_text()
            n = src.count(old)
            if n != 1:
                return mid, name, "NOAPPLY", f"pattern found {n}x in {fname}"
            mutated = src.replace(old, new)
            if fname.endswith(".py"):
                try:
                    compile(mutated, fname, "exec")
                except SyntaxError as e:
                    # A syntax break turns every test red and proves nothing.
                    return mid, name, "INVALID", f"mutated {fname} does not compile: {e}"
            f.write_text(mutated)
        rc = run(sel, root)
    verdict = {1: "CAUGHT", 0: "MISSED", 5: "NOSELECT"}.get(rc, f"ERROR(rc={rc})")
    return mid, name, verdict, f"-k '{sel}'"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("only", nargs="*", help="run only mutation ids starting with these")
    ap.add_argument("-j", type=int, default=os.cpu_count() or 2)
    args = ap.parse_args()
    muts = [m for m in MUTATIONS if not args.only or any(m[0].startswith(o) for o in args.only)]
    ids = [m[0] for m in MUTATIONS]
    assert len(ids) == len(set(ids)), "duplicate mutation id"

    print(f"  known red (deselected, not mutation-checked): {len(KNOWN_RED)}")
    for t in KNOWN_RED:
        print(f"    - {t}")
    base = run(" or ".join(f"({m[5]})" for m in muts), REPO)
    print(f"  baseline (unmutated) selected tests: {'GREEN' if base == 0 else f'RED rc={base} -- fix first'}")
    if base != 0:
        return 1

    results = {}
    with cf.ThreadPoolExecutor(max_workers=max(1, args.j)) as pool:
        for mid, name, verdict, detail in pool.map(check, muts):
            results[mid] = verdict
            extra = f"  [{detail}]" if verdict != "CAUGHT" else ""
            print(f"  {verdict:8s} {mid:22s} {name}{extra}", flush=True)
    bad = [k for k, v in results.items() if v != "CAUGHT"]
    print(f"\n  {len(muts) - len(bad)}/{len(muts)} caught")
    if bad:
        print("  not caught: " + ", ".join(f"{k}={results[k]}" for k in bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
