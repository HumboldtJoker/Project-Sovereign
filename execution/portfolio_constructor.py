"""
Portfolio Construction Module
==============================
Takes the agent's conviction list and builds a properly diversified
allocation — sector-balanced, correlation-aware, regime-appropriate.

Enforces a minimum position count but lets the agent decide what to hold.

Author: CC (Coalition Code)
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

from core.config import (
    MAX_POSITION_PCT,
    CASH_RESERVE_PCT,
    REGIME_POSITION_MODIFIERS,
)

logger = logging.getLogger(__name__)

# ── Portfolio Construction Rules ─────────────────────────────────────────────

MIN_POSITIONS = 4           # Minimum for diversification
MAX_POSITION_PCT_SINGLE = MAX_POSITION_PCT  # 30% cap per position
MIN_POSITION_PCT = 0.05     # 5% minimum — don't bother with dust positions
TARGET_CASH_PCT = {
    "BULLISH": 0.10,        # 10% cash in bull markets
    "NEUTRAL": 0.20,        # 20% cash in neutral
    "CAUTIOUS": 0.40,       # 40% cash when cautious
    "BEARISH": 0.60,        # 60% cash in bear markets
    "CRITICAL": 1.00,       # 100% cash in crisis
}


def assess_concentration(positions: List[Dict], portfolio_value: float) -> Dict:
    """
    Assess current portfolio concentration and return recommendations.

    Args:
        positions: List of current positions [{ticker, market_value, sector, ...}]
        portfolio_value: Total portfolio value

    Returns:
        Assessment dict with concentration metrics and issues
    """
    if portfolio_value <= 0:
        return {"healthy": False, "issues": ["Portfolio value is zero"]}

    issues = []
    n_positions = len(positions)

    # Check minimum positions
    if n_positions < MIN_POSITIONS and portfolio_value > 1000:
        issues.append(
            f"Under-diversified: {n_positions} positions, minimum is {MIN_POSITIONS}. "
            f"Spread risk across at least {MIN_POSITIONS} uncorrelated assets."
        )

    # Check single-position concentration
    for pos in positions:
        weight = pos.get("market_value", 0) / portfolio_value
        if weight > MAX_POSITION_PCT_SINGLE:
            issues.append(
                f"{pos.get('ticker', '?')} is {weight*100:.1f}% of portfolio "
                f"(max {MAX_POSITION_PCT_SINGLE*100}%). Trim or add other positions."
            )

    # Check sector concentration
    sector_weights = {}
    for pos in positions:
        sector = pos.get("sector", "Unknown")
        weight = pos.get("market_value", 0) / portfolio_value
        sector_weights[sector] = sector_weights.get(sector, 0) + weight

    for sector, weight in sector_weights.items():
        if weight > 0.40:
            issues.append(
                f"{sector} sector is {weight*100:.1f}% of portfolio. "
                f"Add exposure to other sectors."
            )

    # Check cash level
    cash = portfolio_value - sum(p.get("market_value", 0) for p in positions)
    cash_pct = cash / portfolio_value

    return {
        "healthy": len(issues) == 0,
        "n_positions": n_positions,
        "cash_pct": round(cash_pct * 100, 1),
        "sector_weights": {k: round(v * 100, 1) for k, v in sector_weights.items()},
        "issues": issues,
    }


def build_allocation(
    conviction_list: List[Dict],
    portfolio_value: float,
    current_positions: List[Dict],
    regime: str = "NEUTRAL",
    cash_balance: float = 0,
) -> List[Dict]:
    """
    Build a diversified allocation from the agent's conviction list.

    Args:
        conviction_list: Ranked list of tickers with conviction scores
            [{ticker, conviction (0-1), sector, current_price, reasoning}]
        portfolio_value: Total portfolio value
        current_positions: What we already hold
        regime: Current market regime
        cash_balance: Current cash

    Returns:
        List of trade recommendations:
            [{ticker, action, quantity, rationale}]
    """
    regime_modifier = REGIME_POSITION_MODIFIERS.get(regime, 0.75)
    target_cash = TARGET_CASH_PCT.get(regime, 0.20)

    # In CRITICAL regime, liquidate everything
    if regime == "CRITICAL":
        trades = []
        for pos in current_positions:
            trades.append({
                "ticker": pos.get("ticker"),
                "action": "SELL",
                "quantity": pos.get("quantity", 0),
                "rationale": "CRITICAL regime — preserve capital at all costs (Taleb)",
            })
        return trades

    # Available capital for equity allocation
    investable = portfolio_value * (1 - target_cash)
    current_equity = sum(p.get("market_value", 0) for p in current_positions)

    # How much we need to deploy
    target_equity = investable
    deploy_budget = max(0, target_equity - current_equity)

    if deploy_budget < portfolio_value * MIN_POSITION_PCT:
        logger.info("Portfolio already near target allocation, no rebalancing needed")
        return []

    # Build target allocation from conviction list
    # Filter to candidates we don't already hold (or hold under-weight)
    held_tickers = {p.get("ticker") for p in current_positions}

    candidates = []
    for c in conviction_list:
        ticker = c.get("ticker", "")
        if not ticker:
            continue

        conviction = c.get("conviction", 0.5)
        price = c.get("current_price", 0)
        if price <= 0:
            continue

        # Weight by conviction, capped by max position
        raw_weight = conviction * regime_modifier
        weight = min(raw_weight, MAX_POSITION_PCT_SINGLE)
        weight = max(weight, MIN_POSITION_PCT)

        candidates.append({
            "ticker": ticker,
            "weight": weight,
            "price": price,
            "sector": c.get("sector", "Unknown"),
            "reasoning": c.get("reasoning", ""),
            "already_held": ticker in held_tickers,
        })

    if not candidates:
        return []

    # Normalize weights to sum to investable amount
    total_weight = sum(c["weight"] for c in candidates)
    if total_weight > 0:
        for c in candidates:
            c["weight"] = c["weight"] / total_weight

    # Ensure minimum position count
    n_new = sum(1 for c in candidates if not c["already_held"])
    n_held = len(held_tickers)
    if n_held + n_new < MIN_POSITIONS and len(candidates) >= MIN_POSITIONS:
        # Force at least MIN_POSITIONS candidates
        candidates = candidates[:max(MIN_POSITIONS - n_held, 1)]
        # Re-normalize
        total_weight = sum(c["weight"] for c in candidates)
        if total_weight > 0:
            for c in candidates:
                c["weight"] = c["weight"] / total_weight

    # Generate trade recommendations
    trades = []
    for c in candidates:
        if c["already_held"]:
            continue

        dollar_amount = deploy_budget * c["weight"]
        quantity = int(dollar_amount / c["price"])

        if quantity <= 0:
            continue

        actual_pct = (quantity * c["price"]) / portfolio_value * 100

        trades.append({
            "ticker": c["ticker"],
            "action": "BUY",
            "quantity": quantity,
            "price": c["price"],
            "dollar_amount": round(quantity * c["price"], 2),
            "portfolio_pct": round(actual_pct, 1),
            "sector": c["sector"],
            "rationale": (
                f"Conviction-weighted allocation: {c['reasoning'][:100]}. "
                f"Regime: {regime} ({regime_modifier}x modifier). "
                f"Target cash: {target_cash*100:.0f}%."
            ),
        })

    return trades


def generate_diversification_prompt(assessment: Dict, regime: str) -> str:
    """
    Generate a prompt fragment for the ReAct agent that tells it
    about concentration issues and minimum position requirements.
    """
    if assessment["healthy"]:
        return ""

    lines = [
        "PORTFOLIO ALERT — DIVERSIFICATION REQUIRED:",
        f"Current positions: {assessment['n_positions']} (minimum: {MIN_POSITIONS})",
        f"Cash: {assessment['cash_pct']}%",
    ]

    for issue in assessment["issues"]:
        lines.append(f"  - {issue}")

    target_cash = TARGET_CASH_PCT.get(regime, 0.20) * 100
    lines.append(f"\nTarget for {regime} regime: {target_cash:.0f}% cash, "
                 f"remainder spread across {MIN_POSITIONS}+ positions in different sectors.")
    lines.append("Use your conviction list to build a balanced allocation.")

    return "\n".join(lines)
