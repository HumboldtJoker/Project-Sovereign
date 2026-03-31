"""
Pre-action safety guardrails for the Sovereign.

Wraps the RiskManager's validation with additional autonomous-specific checks
including market-hours enforcement, daily circuit breakers, API-credit gating,
and a full audit trail of every safety check performed in a session.

All output is structured for direct inclusion in agent_log.json safety_checks
fields.
"""

import logging
import re
from datetime import datetime, time as dt_time, timezone
from typing import Dict, List, Optional, Tuple

import pytz

from core.config import (
    CASH_RESERVE_PCT,
    DAILY_LOSS_LIMIT_PCT,
    MAX_POSITION_PCT,
)

# Optional integration with the existing risk manager
try:
    from execution.risk_manager import RiskManager
    HAS_RISK_MANAGER = True
except ImportError:
    HAS_RISK_MANAGER = False

# Optional macro regime overlay
try:
    from analysis.macro import get_macro_regime
    HAS_MACRO = True
except ImportError:
    HAS_MACRO = False

logger = logging.getLogger(__name__)

# Simple pattern: 1-5 uppercase letters, optionally with a dot segment (BRK.B)
_TICKER_RE = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")

# Analysis-rate-limit: max distinct tickers per session before warning
_MAX_ANALYSIS_TICKERS_PER_SESSION = 50


class SafetyGuardrails:
    """
    Autonomous pre-action safety layer.

    Every public method returns structured data suitable for the
    ``safety_checks`` field in agent_log.json.  All checks performed
    during the session lifetime are accumulated in ``self.audit_trail``
    so they can be included in the final execution log.
    """

    def __init__(
        self,
        risk_manager: Optional["RiskManager"] = None,
        max_position_pct: float = MAX_POSITION_PCT,
        daily_loss_limit_pct: float = DAILY_LOSS_LIMIT_PCT,
        cash_reserve_pct: float = CASH_RESERVE_PCT,
    ) -> None:
        """
        Initialise guardrails.

        Args:
            risk_manager: Optional existing RiskManager instance.  If not
                provided a lightweight one is created internally (no
                auto-execution, no macro overlay -- the overlay is done
                here instead).
            max_position_pct: Hard cap for a single position as a fraction
                of portfolio value (default from core.config).
            daily_loss_limit_pct: Percentage drawdown that triggers the
                circuit breaker (default from core.config).
            cash_reserve_pct: Minimum cash as a fraction of portfolio
                value (default from core.config).
        """
        if risk_manager is not None:
            self.risk_manager = risk_manager
        elif HAS_RISK_MANAGER:
            try:
                self.risk_manager = RiskManager(
                    enable_auto_execute=False,
                    enable_macro_overlay=False,
                )
            except Exception as exc:
                logger.warning("Could not initialise RiskManager: %s", exc)
                self.risk_manager = None
        else:
            self.risk_manager = None

        self.max_position_pct = max_position_pct
        self.daily_loss_limit_pct = daily_loss_limit_pct
        self.cash_reserve_pct = cash_reserve_pct

        # Market hours (US Eastern)
        self.market_open = dt_time(9, 30)
        self.market_close = dt_time(16, 0)
        try:
            self.eastern_tz = pytz.timezone("US/Eastern")
        except Exception:
            # Fallback: some minimal pytz installs lack 'US/Eastern'
            self.eastern_tz = pytz.timezone("America/New_York")

        # Session audit trail
        self.audit_trail: List[Dict] = []
        self._analysis_tickers: set = set()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _record(self, check_name: str, passed: bool, detail: str) -> Dict:
        """Record a single safety check and return its structured entry."""
        entry = {
            "check": check_name,
            "passed": passed,
            "detail": detail,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self.audit_trail.append(entry)
        level = logging.DEBUG if passed else logging.WARNING
        logger.log(level, "Safety check [%s] %s: %s",
                   check_name, "PASS" if passed else "FAIL", detail)
        return entry

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def validate_trade(
        self,
        action: str,
        ticker: str,
        quantity: float,
        price: float,
        portfolio_value: float,
        current_positions: dict,
        cash: float,
    ) -> Tuple[bool, str, List[Dict]]:
        """
        Run the full suite of pre-trade safety checks.

        Returns:
            (approved, reason, checks_passed) where *checks_passed* is a
            list of structured dicts ready for agent_log.json.
        """
        action = action.upper()
        checks: List[Dict] = []

        # 1. Ticker format sanity
        if not _TICKER_RE.match(ticker.upper()):
            c = self._record("ticker_format", False,
                             f"Invalid ticker format: {ticker}")
            checks.append(c)
            return False, c["detail"], checks
        checks.append(self._record("ticker_format", True,
                                   f"Ticker {ticker} format OK"))

        # 2. Market hours (warn but do not block -- after-hours orders
        #    may be intentional for next-open execution)
        mkt_open, mkt_msg = self.check_market_hours()
        checks.append(self._record("market_hours", mkt_open, mkt_msg))

        # 3. Circuit breaker / daily loss
        if self.risk_manager and self.risk_manager.circuit_breaker_triggered:
            c = self._record("circuit_breaker", False,
                             "Circuit breaker active -- all trading halted")
            checks.append(c)
            return False, c["detail"], checks
        checks.append(self._record("circuit_breaker", True,
                                   "Circuit breaker not triggered"))

        # 4. Cash reserve enforcement (buys only)
        if action == "BUY":
            order_value = quantity * price
            post_trade_cash = cash - order_value
            min_cash = portfolio_value * self.cash_reserve_pct
            if post_trade_cash < min_cash:
                detail = (
                    f"Post-trade cash ${post_trade_cash:,.2f} would fall below "
                    f"required reserve ${min_cash:,.2f} "
                    f"({self.cash_reserve_pct * 100:.0f}% of portfolio)"
                )
                c = self._record("cash_reserve", False, detail)
                checks.append(c)
                return False, detail, checks
            checks.append(self._record(
                "cash_reserve", True,
                f"Post-trade cash ${post_trade_cash:,.2f} >= reserve ${min_cash:,.2f}"))

        # 5. Position concentration
        if action == "BUY":
            existing_value = 0.0
            pos = current_positions.get(ticker)
            if pos is not None:
                # Support both Position dataclass and plain dict
                qty = getattr(pos, "quantity", None)
                if qty is None:
                    qty = pos.get("quantity", 0) if isinstance(pos, dict) else 0
                existing_value = qty * price
            new_position_value = existing_value + (quantity * price)
            position_pct = (
                (new_position_value / portfolio_value) if portfolio_value > 0 else 1.0
            )
            if position_pct > self.max_position_pct:
                detail = (
                    f"Position {ticker} would be {position_pct * 100:.1f}% of "
                    f"portfolio, exceeding {self.max_position_pct * 100:.0f}% limit"
                )
                c = self._record("position_concentration", False, detail)
                checks.append(c)
                return False, detail, checks
            checks.append(self._record(
                "position_concentration", True,
                f"{ticker} at {position_pct * 100:.1f}% -- within "
                f"{self.max_position_pct * 100:.0f}% limit"))

        # 6. Delegate to RiskManager.validate_order for remaining checks
        if self.risk_manager is not None:
            rm_valid, rm_reason = self.risk_manager.validate_order(
                action=action,
                ticker=ticker,
                quantity=int(quantity),
                price=price,
                portfolio_value=portfolio_value,
                current_positions=current_positions,
                cash=cash,
            )
            if not rm_valid:
                c = self._record("risk_manager_validation", False,
                                 rm_reason or "Rejected by risk manager")
                checks.append(c)
                return False, c["detail"], checks
            checks.append(self._record("risk_manager_validation", True,
                                       "Passed risk manager order validation"))

        # 7. Macro regime check (soft block in CRITICAL regime)
        if HAS_MACRO and action == "BUY":
            try:
                regime = get_macro_regime()
                regime_name = regime.get("regime", "UNKNOWN")
                modifier = regime.get("risk_modifier", 1.0)
                if regime_name == "CRITICAL":
                    detail = (
                        f"Macro regime is CRITICAL (modifier={modifier}) "
                        f"-- new BUY orders blocked"
                    )
                    c = self._record("macro_regime", False, detail)
                    checks.append(c)
                    return False, detail, checks
                checks.append(self._record(
                    "macro_regime", True,
                    f"Regime {regime_name}, modifier {modifier}"))
            except Exception as exc:
                # Macro failure is non-fatal; log and continue
                checks.append(self._record(
                    "macro_regime", True,
                    f"Macro check unavailable ({exc}); proceeding"))

        # All checks passed
        logger.info("Trade APPROVED: %s %s x%.4f @ $%.2f",
                    action, ticker, quantity, price)
        return True, "All safety checks passed", checks

    def validate_analysis_request(self, ticker: str) -> Tuple[bool, str]:
        """
        Gate analysis requests to avoid burning API credits on bogus
        or duplicate tickers.

        Returns:
            (approved, reason)
        """
        ticker = ticker.upper()

        if not _TICKER_RE.match(ticker):
            self._record("analysis_ticker_format", False,
                         f"Invalid ticker: {ticker}")
            return False, f"Invalid ticker format: {ticker}"

        self._analysis_tickers.add(ticker)
        if len(self._analysis_tickers) > _MAX_ANALYSIS_TICKERS_PER_SESSION:
            detail = (
                f"Session has analysed {len(self._analysis_tickers)} tickers, "
                f"exceeding soft limit of {_MAX_ANALYSIS_TICKERS_PER_SESSION}"
            )
            self._record("analysis_rate_limit", False, detail)
            return False, detail

        self._record("analysis_request", True,
                     f"Analysis approved for {ticker} "
                     f"({len(self._analysis_tickers)} tickers this session)")
        return True, f"Approved: {ticker}"

    def check_market_hours(self) -> Tuple[bool, str]:
        """
        Determine whether US equity markets are currently open.

        Returns:
            (is_open, human_readable_message)
        """
        now_et = datetime.now(self.eastern_tz)
        current_time = now_et.time()

        # Weekend check (Monday=0 ... Sunday=6)
        if now_et.weekday() >= 5:
            msg = f"Markets closed (weekend). Current ET: {now_et.strftime('%A %H:%M')}"
            self._record("market_hours", False, msg)
            return False, msg

        is_open = self.market_open <= current_time <= self.market_close
        if is_open:
            msg = f"Markets OPEN. ET: {now_et.strftime('%H:%M')}"
        else:
            msg = (
                f"Markets CLOSED. ET: {now_et.strftime('%H:%M')}. "
                f"Hours: {self.market_open.strftime('%H:%M')}-"
                f"{self.market_close.strftime('%H:%M')}"
            )

        self._record("market_hours", is_open, msg)
        return is_open, msg

    def check_daily_limits(
        self, current_value: float, starting_value: float
    ) -> Tuple[bool, str]:
        """
        Circuit breaker: halt trading if intraday drawdown exceeds the
        configured limit.

        Returns:
            (within_limits, message).  ``within_limits`` is True when
            the portfolio is still within acceptable drawdown.
        """
        if starting_value <= 0:
            msg = "Starting value is zero -- cannot compute drawdown"
            self._record("daily_limits", False, msg)
            return False, msg

        drawdown = (starting_value - current_value) / starting_value
        drawdown_pct = drawdown * 100
        limit_pct = self.daily_loss_limit_pct * 100

        within_limits = drawdown < self.daily_loss_limit_pct

        if within_limits:
            msg = (
                f"Daily drawdown {drawdown_pct:+.2f}% within "
                f"{limit_pct:.1f}% limit"
            )
        else:
            msg = (
                f"CIRCUIT BREAKER: drawdown {drawdown_pct:+.2f}% "
                f"exceeds {limit_pct:.1f}% limit -- trading halted"
            )
            # Also trip the underlying risk manager if available
            if self.risk_manager is not None:
                self.risk_manager.circuit_breaker_triggered = True

        self._record("daily_limits", within_limits, msg)
        return within_limits, msg

    def get_safety_report(self) -> Dict:
        """
        Produce a session-level safety summary suitable for inclusion in
        the top-level agent_log.json.

        Returns:
            Dict with counts, pass-rate, and the full ordered audit trail.
        """
        total = len(self.audit_trail)
        passed = sum(1 for c in self.audit_trail if c["passed"])
        failed = total - passed

        # Group failures by check name for quick triage
        failure_summary: Dict[str, int] = {}
        for c in self.audit_trail:
            if not c["passed"]:
                name = c["check"]
                failure_summary[name] = failure_summary.get(name, 0) + 1

        return {
            "total_checks": total,
            "passed": passed,
            "failed": failed,
            "pass_rate": round(passed / total, 4) if total else 1.0,
            "unique_tickers_analysed": len(self._analysis_tickers),
            "failure_summary": failure_summary,
            "audit_trail": self.audit_trail,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
