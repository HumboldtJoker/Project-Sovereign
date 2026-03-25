"""
Portfolio Manager for PLGenesis Market Agent.

Tracks cash, positions, trades, and P&L for both paper and live trading modes.
"""

import json
import logging
from datetime import datetime
from typing import Dict, List, Optional
from dataclasses import dataclass, asdict
from pathlib import Path

from core.config import DATA_DIR

logger = logging.getLogger(__name__)


@dataclass
class Position:
    """Represents a stock position in the portfolio."""
    ticker: str
    quantity: float
    avg_cost: float
    current_price: float = 0.0

    @property
    def market_value(self) -> float:
        """Current market value of position."""
        return self.quantity * self.current_price

    @property
    def cost_basis(self) -> float:
        """Total cost basis of position."""
        return self.quantity * self.avg_cost

    @property
    def unrealized_pl(self) -> float:
        """Unrealized profit/loss."""
        return self.market_value - self.cost_basis

    @property
    def unrealized_pl_percent(self) -> float:
        """Unrealized P&L as percentage."""
        if self.cost_basis == 0:
            return 0.0
        return (self.unrealized_pl / self.cost_basis) * 100


@dataclass
class Trade:
    """Represents a completed trade."""
    timestamp: str
    ticker: str
    action: str  # 'BUY' or 'SELL'
    quantity: float
    price: float
    commission: float = 0.0

    @property
    def total_cost(self) -> float:
        """Total cost including commission."""
        return (self.quantity * self.price) + self.commission

    @property
    def net_amount(self) -> float:
        """Net amount (negative for buys, positive for sells)."""
        amount = self.quantity * self.price
        if self.action == 'BUY':
            return -(amount + self.commission)
        else:
            return amount - self.commission


class PortfolioManager:
    """
    Manages portfolio state for PLGenesis Market Agent.

    Supports both paper trading (simulated) and live trading (Alpaca API).
    """

    def __init__(self, mode: str = "local", initial_cash: float = 100000.0,
                 storage_path: str = None):
        """
        Initialize portfolio manager.

        Args:
            mode: Trading mode
                - 'local': Fully simulated trading (no API calls)
                - 'alpaca': Use Alpaca API (paper vs live determined by ALPACA_PAPER config)
                - 'paper'/'live': Deprecated aliases for 'local'/'alpaca'
            initial_cash: Starting cash balance (local mode only)
            storage_path: Path to save portfolio state (local mode only).
                          Defaults to DATA_DIR / "portfolio_state.json".
        """
        self.mode = mode.lower()

        if storage_path is None:
            self.storage_path = DATA_DIR / "portfolio_state.json"
        else:
            self.storage_path = Path(storage_path)

        # Backward compatibility: map old names to new names
        mode_aliases = {'paper': 'local', 'live': 'alpaca'}
        if self.mode in mode_aliases:
            logger.warning(
                "mode='%s' is deprecated. Use mode='%s' instead.",
                self.mode, mode_aliases[self.mode],
            )
            self.mode = mode_aliases[self.mode]

        if self.mode == "local":
            # Local mode: load from file or create new
            if self.storage_path.exists():
                self.load_state()
            else:
                self.cash = initial_cash
                self.initial_cash = initial_cash
                self.positions: Dict[str, Position] = {}
                self.trade_history: List[Trade] = []
                self.save_state()
        elif self.mode == "alpaca":
            # Alpaca mode: will sync with Alpaca API
            self.cash = 0.0  # Will be fetched from Alpaca
            self.initial_cash = 0.0
            self.positions: Dict[str, Position] = {}
            self.trade_history: List[Trade] = []
        else:
            raise ValueError(f"Invalid mode: {mode}. Must be 'local' or 'alpaca'")

    def execute_trade(self, ticker: str, action: str, quantity: float,
                      price: float, commission: float = 0.0) -> Dict:
        """
        Execute a trade and update portfolio.

        Args:
            ticker: Stock symbol
            action: 'BUY', 'SELL', 'SHORT', or 'COVER'
            quantity: Number of shares (fractional shares supported)
            price: Price per share
            commission: Trading commission (default 0 for Alpaca)

        Returns:
            Dict with trade details and updated portfolio state
        """
        action = action.upper()

        if action not in ['BUY', 'SELL', 'SHORT', 'COVER']:
            raise ValueError(f"Invalid action: {action}")

        # Create trade record
        trade = Trade(
            timestamp=datetime.now().isoformat(),
            ticker=ticker,
            action=action,
            quantity=quantity,
            price=price,
            commission=commission,
        )

        # Calculate total cost
        total_cost = trade.total_cost

        if action == 'BUY':
            # Check if we have enough cash
            if self.cash < total_cost:
                raise ValueError(
                    f"Insufficient cash: have ${self.cash:.2f}, need ${total_cost:.2f}"
                )

            # Deduct cash
            self.cash -= total_cost

            # Add or update position
            if ticker in self.positions:
                # Average up/down the cost basis
                pos = self.positions[ticker]
                new_quantity = pos.quantity + quantity
                new_avg_cost = (
                    (pos.quantity * pos.avg_cost) + (quantity * price)
                ) / new_quantity

                pos.quantity = new_quantity
                pos.avg_cost = new_avg_cost
            else:
                # New position
                self.positions[ticker] = Position(
                    ticker=ticker,
                    quantity=quantity,
                    avg_cost=price,
                    current_price=price,
                )

        elif action == 'SELL':
            # Check if we have the position
            if ticker not in self.positions:
                raise ValueError(f"No position in {ticker} to sell")

            pos = self.positions[ticker]

            # Check if we have enough shares
            if pos.quantity < quantity:
                raise ValueError(
                    f"Insufficient shares: have {pos.quantity}, trying to sell {quantity}"
                )

            # Calculate realized P&L
            realized_pl = (price - pos.avg_cost) * quantity
            logger.info(
                "Realized P&L on %s SELL: $%.2f", ticker, realized_pl,
            )

            # Add cash (proceeds minus commission)
            proceeds = (quantity * price) - commission
            self.cash += proceeds

            # Update or remove position
            pos.quantity -= quantity
            if pos.quantity == 0:
                del self.positions[ticker]

        elif action == 'SHORT':
            # Short selling: borrow and sell shares
            # Receive proceeds from the sale
            proceeds = (quantity * price) - commission
            self.cash += proceeds

            # Create or add to short position (negative quantity)
            if ticker in self.positions:
                pos = self.positions[ticker]
                if pos.quantity > 0:
                    raise ValueError(f"Cannot SHORT {ticker}: have existing long position")
                # Average down the short
                new_quantity = pos.quantity - quantity  # More negative
                # Weighted average of short prices
                new_avg_cost = (
                    (abs(pos.quantity) * pos.avg_cost) + (quantity * price)
                ) / abs(new_quantity)
                pos.quantity = new_quantity
                pos.avg_cost = new_avg_cost
            else:
                # New short position (negative quantity)
                self.positions[ticker] = Position(
                    ticker=ticker,
                    quantity=-quantity,  # Negative for short
                    avg_cost=price,      # Price we shorted at
                    current_price=price,
                )

        elif action == 'COVER':
            # Cover short: buy back borrowed shares
            if ticker not in self.positions:
                raise ValueError(f"No short position in {ticker} to cover")

            pos = self.positions[ticker]
            if pos.quantity >= 0:
                raise ValueError(f"Cannot COVER {ticker}: position is long, not short")

            # Check if covering more than shorted
            if quantity > abs(pos.quantity):
                raise ValueError(
                    f"Cannot cover {quantity} shares: only short {abs(pos.quantity)}"
                )

            # Calculate realized P&L on short
            # Profit = (short price - cover price) * quantity
            realized_pl = (pos.avg_cost - price) * quantity
            logger.info(
                "Realized P&L on %s COVER: $%.2f", ticker, realized_pl,
            )

            # Deduct cash to buy back shares
            cost = (quantity * price) + commission
            if self.cash < cost:
                raise ValueError(
                    f"Insufficient cash to cover: have ${self.cash:.2f}, need ${cost:.2f}"
                )
            self.cash -= cost

            # Update or close position
            pos.quantity += quantity  # Becomes less negative
            if pos.quantity == 0:
                del self.positions[ticker]

        # Add to trade history
        self.trade_history.append(trade)

        # Save state (paper mode only)
        if self.mode == "local":
            self.save_state()

        return {
            "success": True,
            "trade": asdict(trade),
            "cash": self.cash,
            "portfolio_value": self.get_portfolio_value(),
        }

    def update_prices(self, prices: Dict[str, float]) -> None:
        """
        Update current prices for all positions.

        Args:
            prices: Dict mapping ticker -> current price
        """
        for ticker, price in prices.items():
            if ticker in self.positions:
                self.positions[ticker].current_price = price

    def get_position(self, ticker: str) -> Optional[Position]:
        """Get position for a specific ticker."""
        return self.positions.get(ticker)

    def get_all_positions(self) -> Dict[str, Position]:
        """Get all positions."""
        return self.positions.copy()

    def get_portfolio_value(self) -> float:
        """
        Calculate total portfolio value (cash + positions).

        Returns:
            Total portfolio value in dollars
        """
        positions_value = sum(
            pos.market_value for pos in self.positions.values()
        )
        return self.cash + positions_value

    def get_portfolio_summary(self) -> Dict:
        """
        Get comprehensive portfolio summary.

        Returns:
            Dict with cash, positions, total value, and performance metrics
        """
        portfolio_value = self.get_portfolio_value()

        # Calculate total unrealized P&L
        total_unrealized_pl = sum(
            pos.unrealized_pl for pos in self.positions.values()
        )

        # Calculate total return
        if self.initial_cash > 0:
            total_return = (
                (portfolio_value - self.initial_cash) / self.initial_cash
            ) * 100
        else:
            total_return = 0.0

        # Build positions summary
        positions_summary = []
        for ticker, pos in self.positions.items():
            positions_summary.append({
                "ticker": ticker,
                "quantity": pos.quantity,
                "avg_cost": round(pos.avg_cost, 2),
                "current_price": round(pos.current_price, 2),
                "market_value": round(pos.market_value, 2),
                "unrealized_pl": round(pos.unrealized_pl, 2),
                "unrealized_pl_percent": round(pos.unrealized_pl_percent, 2),
            })

        return {
            "mode": self.mode,
            "cash": round(self.cash, 2),
            "positions_value": round(
                sum(pos.market_value for pos in self.positions.values()), 2
            ),
            "total_value": round(portfolio_value, 2),
            "initial_value": round(self.initial_cash, 2),
            "total_return": round(total_return, 2),
            "total_unrealized_pl": round(total_unrealized_pl, 2),
            "num_positions": len(self.positions),
            "positions": positions_summary,
            "num_trades": len(self.trade_history),
        }

    def get_trade_history(self, limit: Optional[int] = None) -> List[Dict]:
        """
        Get trade history.

        Args:
            limit: Maximum number of trades to return (most recent first)

        Returns:
            List of trade dicts
        """
        trades = [asdict(trade) for trade in self.trade_history]
        trades.reverse()  # Most recent first

        if limit:
            trades = trades[:limit]

        return trades

    def save_state(self) -> None:
        """Save portfolio state to JSON (paper mode only)."""
        if self.mode != "local":
            return

        state = {
            "cash": self.cash,
            "initial_cash": self.initial_cash,
            "positions": {
                ticker: {
                    "ticker": pos.ticker,
                    "quantity": pos.quantity,
                    "avg_cost": pos.avg_cost,
                    "current_price": pos.current_price,
                }
                for ticker, pos in self.positions.items()
            },
            "trade_history": [asdict(trade) for trade in self.trade_history],
            "last_updated": datetime.now().isoformat(),
        }

        with open(self.storage_path, 'w') as f:
            json.dump(state, f, indent=2)

    def load_state(self) -> None:
        """Load portfolio state from JSON (paper mode only)."""
        if self.mode != "local":
            return

        with open(self.storage_path, 'r') as f:
            state = json.load(f)

        self.cash = state['cash']
        self.initial_cash = state['initial_cash']

        # Load positions
        self.positions = {
            ticker: Position(**pos_data)
            for ticker, pos_data in state['positions'].items()
        }

        # Load trade history
        self.trade_history = [
            Trade(**trade_data)
            for trade_data in state['trade_history']
        ]

    def reset(self, initial_cash: float = 100000.0) -> None:
        """
        Reset portfolio to initial state (paper mode only).

        Args:
            initial_cash: Starting cash balance
        """
        if self.mode != "local":
            raise ValueError("Cannot reset live portfolio")

        self.cash = initial_cash
        self.initial_cash = initial_cash
        self.positions = {}
        self.trade_history = []
        self.save_state()
