"""
Risk Manager for PLGenesis Market Agent.

Handles position sizing, stop-losses, and circuit breakers to protect capital.
Supports automated stop-loss execution with comprehensive safety mechanisms.
"""

import logging
import time
from typing import Dict, Optional, Tuple, List
from datetime import datetime, time as dt_time

import pytz

from core.config import (
    MAX_POSITION_PCT,
    DEFAULT_STOP_LOSS_PCT,
    DAILY_LOSS_LIMIT_PCT,
    CASH_RESERVE_PCT,
)

# Optional macro agent integration
try:
    from analysis.macro import MacroAgent
    HAS_MACRO_AGENT = True
except ImportError:
    HAS_MACRO_AGENT = False

logger = logging.getLogger(__name__)


class RiskManager:
    """
    Manages trading risk through position sizing, stop-losses, and circuit breakers.

    Key Features:
    - Position sizing based on portfolio value and risk tolerance
    - Maximum position concentration limits
    - Daily loss circuit breakers
    - Stop-loss recommendations
    - Risk-adjusted order validation
    """

    def __init__(self, investor_profile=None,
                 enable_auto_execute: bool = False,
                 order_executor=None,
                 enable_macro_overlay: bool = True):
        """
        Initialize risk manager.

        Args:
            investor_profile: Optional investor profile for personalized risk settings
            enable_auto_execute: Enable automated stop-loss execution (default: False)
            order_executor: OrderExecutor instance for executing trades
                            (required if enable_auto_execute=True)
            enable_macro_overlay: Enable macro economic regime adjustments (default: True)
        """
        self.profile = investor_profile

        # Macro agent for market regime detection
        self.enable_macro_overlay = enable_macro_overlay
        self.macro_agent = None
        if enable_macro_overlay and HAS_MACRO_AGENT:
            try:
                self.macro_agent = MacroAgent()
                if self.macro_agent.fred is None:
                    logger.warning("MacroAgent initialized but FRED API unavailable")
            except Exception as e:
                logger.warning("Failed to initialize MacroAgent: %s", e)

        # Auto-execution settings
        self.enable_auto_execute = enable_auto_execute
        self.order_executor = order_executor

        if self.enable_auto_execute and not self.order_executor:
            raise ValueError("order_executor required when enable_auto_execute=True")

        # Default risk parameters (can be overridden by investor profile)
        if investor_profile and hasattr(investor_profile, 'profile'):
            risk_tolerance = investor_profile.profile.get('risk_tolerance', 3)
            # Map risk tolerance (1-5) to max position size (5-25%)
            self.max_position_size = 0.05 + (risk_tolerance - 1) * 0.05
            # Map to daily loss limit (1-5%)
            self.daily_loss_limit = 0.01 + (risk_tolerance - 1) * 0.01
        else:
            # Use centralized config defaults
            self.max_position_size = MAX_POSITION_PCT
            self.daily_loss_limit = DAILY_LOSS_LIMIT_PCT

        # Hard limits (never exceed regardless of risk tolerance)
        self.absolute_max_position = 0.25  # 25% max
        self.absolute_daily_loss_limit = 0.05  # 5% max daily loss

        # Tracking
        self.daily_starting_value = None
        self.circuit_breaker_triggered = False

        # Auto-execute tracking
        self.daily_auto_sells = 0
        self.max_daily_auto_sells = 10
        self.confirmation_delay_seconds = 5
        self.auto_execute_log = []

        # Market hours (US Eastern Time)
        self.market_open = dt_time(9, 30)  # 9:30 AM
        self.market_close = dt_time(16, 0)  # 4:00 PM
        self.eastern_tz = pytz.timezone('US/Eastern')

    def calculate_position_size(self, portfolio_value: float,
                                ticker: str,
                                current_price: float,
                                risk_per_trade: Optional[float] = None,
                                apply_macro_overlay: bool = True) -> Dict:
        """
        Calculate recommended position size for a trade.

        Args:
            portfolio_value: Total portfolio value
            ticker: Stock symbol
            current_price: Current stock price
            risk_per_trade: Optional risk amount per trade (defaults to 1% of portfolio)
            apply_macro_overlay: Whether to apply macro regime adjustment (default: True)

        Returns:
            Dict with recommended shares and position details
        """
        if risk_per_trade is None:
            risk_per_trade = 0.01  # 1% default risk per trade

        # Calculate maximum dollar amount for this position
        max_position_dollars = portfolio_value * self.max_position_size

        # Apply macro overlay if enabled
        macro_modifier = 1.0
        macro_regime = None
        macro_recommendation = None

        if apply_macro_overlay and self.macro_agent and self.enable_macro_overlay:
            try:
                regime_data = self.macro_agent.get_market_regime()
                macro_modifier = regime_data.get('risk_modifier', 1.0)
                macro_regime = regime_data.get('regime', 'UNKNOWN')
                macro_recommendation = regime_data.get('recommendation', '')

                # Apply the modifier to position size
                max_position_dollars = max_position_dollars * macro_modifier
            except Exception as e:
                logger.warning("Macro overlay failed, using default: %s", e)

        # Calculate shares based on price
        max_shares = int(max_position_dollars / current_price)

        # Calculate position as percentage
        position_value = max_shares * current_price
        position_pct = (position_value / portfolio_value) * 100 if portfolio_value > 0 else 0

        result = {
            "ticker": ticker,
            "recommended_shares": max_shares,
            "position_value": round(position_value, 2),
            "position_pct": round(position_pct, 2),
            "max_position_pct": round(self.max_position_size * 100, 2),
            "current_price": round(current_price, 2),
            "portfolio_value": round(portfolio_value, 2),
        }

        # Add macro overlay info if applied
        if macro_modifier != 1.0 or macro_regime:
            result["macro_overlay"] = {
                "applied": True,
                "regime": macro_regime,
                "modifier": macro_modifier,
                "recommendation": macro_recommendation,
                "unadjusted_shares": int((portfolio_value * self.max_position_size) / current_price),
            }
        else:
            result["macro_overlay"] = {"applied": False}

        return result

    def validate_order(self, action: str, ticker: str, quantity: int,
                       price: float, portfolio_value: float,
                       current_positions: Dict, cash: float) -> Tuple[bool, Optional[str]]:
        """
        Validate if an order meets risk management criteria.

        Args:
            action: 'BUY' or 'SELL'
            ticker: Stock symbol
            quantity: Number of shares
            price: Order price
            portfolio_value: Total portfolio value
            current_positions: Dict of current positions
            cash: Available cash

        Returns:
            Tuple of (is_valid: bool, rejection_reason: Optional[str])
        """
        action = action.upper()

        # Check circuit breaker
        if self.circuit_breaker_triggered:
            return False, "Circuit breaker triggered - trading halted for today"

        if action == 'BUY':
            # Calculate order value
            order_value = quantity * price

            # Check if we have enough cash
            if order_value > cash:
                return False, f"Insufficient cash: need ${order_value:.2f}, have ${cash:.2f}"

            # Check position size limits
            current_position = current_positions.get(ticker)
            if current_position:
                new_quantity = current_position.quantity + quantity
                new_position_value = new_quantity * price
            else:
                new_position_value = order_value

            position_pct = (new_position_value / portfolio_value) * 100 if portfolio_value > 0 else 0

            if position_pct > (self.max_position_size * 100):
                return False, (
                    f"Position size {position_pct:.1f}% exceeds limit of "
                    f"{self.max_position_size * 100:.1f}%"
                )

            # Check absolute max
            if position_pct > (self.absolute_max_position * 100):
                return False, (
                    f"Position size {position_pct:.1f}% exceeds absolute limit of "
                    f"{self.absolute_max_position * 100:.1f}%"
                )

            return True, None

        elif action == 'SELL':
            # Check if we have the position
            current_position = current_positions.get(ticker)
            if not current_position:
                return False, f"No position in {ticker} to sell"

            # Check if we have enough shares
            if quantity > current_position.quantity:
                return False, (
                    f"Insufficient shares: have {current_position.quantity}, "
                    f"trying to sell {quantity}"
                )

            return True, None

        else:
            return False, f"Invalid action: {action}"

    def check_stop_loss(self, ticker: str, entry_price: float,
                        current_price: float,
                        stop_loss_pct: float = None) -> Tuple[bool, Dict]:
        """
        Check if a position has hit its stop-loss.

        Args:
            ticker: Stock symbol
            entry_price: Average entry price
            current_price: Current market price
            stop_loss_pct: Stop-loss percentage (defaults to DEFAULT_STOP_LOSS_PCT from config)

        Returns:
            Tuple of (should_sell: bool, info: Dict)
        """
        if stop_loss_pct is None:
            stop_loss_pct = DEFAULT_STOP_LOSS_PCT

        loss_pct = ((current_price - entry_price) / entry_price) * 100
        stop_loss_price = entry_price * (1 - stop_loss_pct)

        should_sell = current_price <= stop_loss_price

        return should_sell, {
            "ticker": ticker,
            "entry_price": round(entry_price, 2),
            "current_price": round(current_price, 2),
            "loss_pct": round(loss_pct, 2),
            "stop_loss_price": round(stop_loss_price, 2),
            "stop_loss_pct": round(stop_loss_pct * 100, 2),
            "should_sell": should_sell,
            "reason": "Stop-loss triggered" if should_sell else "Within acceptable range",
        }

    def check_circuit_breaker(self, current_portfolio_value: float) -> Tuple[bool, Dict]:
        """
        Check if daily loss limit has been breached.

        Args:
            current_portfolio_value: Current total portfolio value

        Returns:
            Tuple of (triggered: bool, info: Dict)
        """
        # Set daily starting value on first check of the day
        if self.daily_starting_value is None:
            self.daily_starting_value = current_portfolio_value

        # Calculate daily loss
        daily_loss = self.daily_starting_value - current_portfolio_value
        daily_loss_pct = (daily_loss / self.daily_starting_value) * 100 if self.daily_starting_value > 0 else 0

        # Check if circuit breaker should trigger
        triggered = daily_loss_pct >= (self.daily_loss_limit * 100)

        if triggered and not self.circuit_breaker_triggered:
            self.circuit_breaker_triggered = True
            logger.warning(
                "Circuit breaker triggered: daily loss %.2f%% exceeds limit %.2f%%",
                daily_loss_pct, self.daily_loss_limit * 100,
            )

        return triggered, {
            "daily_starting_value": round(self.daily_starting_value, 2),
            "current_value": round(current_portfolio_value, 2),
            "daily_loss": round(daily_loss, 2),
            "daily_loss_pct": round(daily_loss_pct, 2),
            "loss_limit_pct": round(self.daily_loss_limit * 100, 2),
            "triggered": triggered,
            "message": "CIRCUIT BREAKER TRIGGERED - Trading halted" if triggered else "Within daily loss limits",
        }

    def reset_daily_limits(self, starting_value: float) -> None:
        """
        Reset daily tracking (call at start of trading day).

        Args:
            starting_value: Portfolio value at start of day
        """
        self.daily_starting_value = starting_value
        self.circuit_breaker_triggered = False
        self.daily_auto_sells = 0  # Reset auto-sell counter

    def _is_market_hours(self) -> bool:
        """Check if current time is within market hours (9:30 AM - 4:00 PM ET)."""
        now_et = datetime.now(self.eastern_tz).time()
        return self.market_open <= now_et <= self.market_close

    def _can_auto_execute(self) -> Tuple[bool, Optional[str]]:
        """
        Check if automated execution is allowed.

        Returns:
            Tuple of (allowed: bool, reason: Optional[str])
        """
        if not self.enable_auto_execute:
            return False, "Auto-execution is disabled"

        if self.circuit_breaker_triggered:
            return False, "Circuit breaker is active"

        if not self._is_market_hours():
            return False, "Outside market hours (9:30 AM - 4:00 PM ET)"

        if self.daily_auto_sells >= self.max_daily_auto_sells:
            return False, f"Daily auto-sell limit reached ({self.max_daily_auto_sells})"

        return True, None

    # NOTE: monitor_and_execute_stops was removed -
    # execution_monitor.py handles stop-loss execution directly

    def get_risk_summary(self) -> Dict:
        """Get current risk management settings."""
        summary = {
            "max_position_size_pct": round(self.max_position_size * 100, 2),
            "daily_loss_limit_pct": round(self.daily_loss_limit * 100, 2),
            "absolute_max_position_pct": round(self.absolute_max_position * 100, 2),
            "absolute_daily_loss_limit_pct": round(self.absolute_daily_loss_limit * 100, 2),
            "circuit_breaker_active": self.circuit_breaker_triggered,
            "daily_starting_value": round(self.daily_starting_value, 2) if self.daily_starting_value else None,
        }

        # Add macro overlay info
        if self.enable_macro_overlay and self.macro_agent:
            try:
                regime_data = self.macro_agent.get_market_regime()
                summary["macro_overlay"] = {
                    "enabled": True,
                    "regime": regime_data.get('regime', 'UNKNOWN'),
                    "risk_modifier": regime_data.get('risk_modifier', 1.0),
                    "recommendation": regime_data.get('recommendation', ''),
                    "warnings": regime_data.get('warnings', []),
                }
            except Exception as e:
                summary["macro_overlay"] = {
                    "enabled": True,
                    "error": str(e),
                }
        else:
            summary["macro_overlay"] = {"enabled": False}

        # Add auto-execute info if enabled
        if self.enable_auto_execute:
            summary.update({
                "auto_execute_enabled": True,
                "daily_auto_sells": self.daily_auto_sells,
                "max_daily_auto_sells": self.max_daily_auto_sells,
                "confirmation_delay_seconds": self.confirmation_delay_seconds,
                "market_hours": f"{self.market_open.strftime('%H:%M')} - {self.market_close.strftime('%H:%M')} ET",
                "is_market_hours": self._is_market_hours(),
                "can_auto_execute": self._can_auto_execute()[0],
                "total_auto_executions": len(self.auto_execute_log),
            })
        else:
            summary["auto_execute_enabled"] = False

        return summary

    def get_macro_report(self) -> Optional[str]:
        """
        Get formatted macro economic report.

        Returns:
            Formatted string report or None if macro agent not available
        """
        if not self.macro_agent:
            return None
        try:
            return self.macro_agent.format_report()
        except Exception as e:
            return f"Error generating macro report: {e}"
