#!/usr/bin/env python3
"""
Sovereign
====================================
Autonomous market analysis with 8-layer safety, multi-source intelligence,
and verifiable execution logging.

Entry point for both autonomous and interactive modes.

Usage:
    # Autonomous decision loop (Agent Only track)
    python main.py --autonomous

    # Single analysis query
    python main.py --query "Analyze NVDA for short-term entry"

    # Run market scan
    python main.py --scan
"""

import argparse
import json
import logging
import sys

from core.config import ANTHROPIC_API_KEY, ALPACA_PAPER
from core.react_agent import ReActAgent
from core.tool_registry import Tool
from core.decision_loop import DecisionLoop
from audit_log.structured_logger import save_execution_log, save_canonical_log

# ── Analysis tools ───────────────────────────────────────────────────────────
from analysis.technical import get_technical_indicators
from analysis.sentiment import get_news_sentiment
from analysis.congressional import get_congressional_trades
from analysis.congressional_aggregate import get_aggregate_analysis
from analysis.macro import MacroAgent
from analysis.portfolio import get_portfolio_metrics
from analysis.sector import get_sector_allocation

# ── Execution tools ──────────────────────────────────────────────────────────
from execution.risk_manager import RiskManager
from execution.order_executor import OrderExecutor
from execution.portfolio_manager import PortfolioManager

# ── Integrations ─────────────────────────────────────────────────────────────
from integrations.storacha.storage import upload_execution_log, is_cli_available as storacha_available

# ── Memory ───────────────────────────────────────────────────────────────────
try:
    from memory.market_context import enrich_from_run
    HAS_MEMORY = True
except ImportError:
    HAS_MEMORY = False

# ── Narrator ─────────────────────────────────────────────────────────────────
try:
    from core.narrator import Narrator
    HAS_NARRATOR = True
except ImportError:
    HAS_NARRATOR = False


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("sovereign-agent")


def build_agent() -> ReActAgent:
    """Initialize the ReAct agent with all analysis and execution tools."""
    if not ANTHROPIC_API_KEY:
        logger.error("ANTHROPIC_API_KEY not set")
        sys.exit(1)

    agent = ReActAgent()

    # ── Analysis tools ───────────────────────────────────────────────────
    agent.tools.register(Tool(
        name="get_technical_indicators",
        description="Calculate technical indicators (SMA, RSI, MACD, Bollinger) for a stock",
        parameters={"ticker": "string", "period": "string (default: '6mo')"},
        function=get_technical_indicators,
    ))

    agent.tools.register(Tool(
        name="get_news_sentiment",
        description="Analyze recent news sentiment for a stock",
        parameters={"ticker": "string", "days": "int (default: 7)"},
        function=get_news_sentiment,
    ))

    agent.tools.register(Tool(
        name="get_congressional_trades",
        description="Get recent congressional trading activity for a stock (STOCK Act disclosures)",
        parameters={"ticker": "string", "days": "int (default: 90)"},
        function=get_congressional_trades,
    ))

    agent.tools.register(Tool(
        name="get_aggregate_congressional",
        description="Get aggregate congressional trading patterns across all tickers",
        parameters={},
        function=get_aggregate_analysis,
    ))

    macro = MacroAgent()
    agent.tools.register(Tool(
        name="get_market_regime",
        description="Detect current macro regime (VIX, yield curve, credit spreads, unemployment)",
        parameters={},
        function=macro.get_market_regime,
    ))

    agent.tools.register(Tool(
        name="get_portfolio_metrics",
        description="Calculate portfolio correlation, volatility, beta, Sharpe, diversification score",
        parameters={"tickers": "list of strings", "period": "string (default: '1y')"},
        function=get_portfolio_metrics,
    ))

    agent.tools.register(Tool(
        name="get_sector_allocation",
        description="Analyze sector exposure and concentration risk vs S&P 500 benchmark",
        parameters={"tickers": "list of strings"},
        function=get_sector_allocation,
    ))

    # ── Execution tools ──────────────────────────────────────────────────
    executor = OrderExecutor(mode="alpaca")

    agent.tools.register(Tool(
        name="execute_trade",
        description="Execute a trade order (paper or live). Action: 'BUY' or 'SELL'",
        parameters={
            "ticker": "string",
            "action": "string ('BUY' or 'SELL')",
            "quantity": "float",
            "order_type": "string (default: 'market')",
        },
        function=executor.execute_order,
    ))

    agent.tools.register(Tool(
        name="get_portfolio_summary",
        description="Get current portfolio positions, P&L, and cash balance",
        parameters={},
        function=executor.get_portfolio_summary,
    ))

    def _get_stock_price(ticker: str) -> dict:
        """Wrap scalar price into dict for agent tool interface."""
        price = executor.get_current_price(ticker)
        return {"ticker": ticker, "price": price}

    agent.tools.register(Tool(
        name="get_stock_price",
        description="Get current price for a stock",
        parameters={"ticker": "string"},
        function=_get_stock_price,
    ))

    # ── Risk tools ───────────────────────────────────────────────────────
    risk = RiskManager()

    agent.tools.register(Tool(
        name="calculate_position_size",
        description="Calculate risk-adjusted position size with macro overlay",
        parameters={
            "portfolio_value": "float",
            "ticker": "string",
            "current_price": "float",
        },
        function=risk.calculate_position_size,
    ))

    agent.tools.register(Tool(
        name="validate_order",
        description="Validate a proposed trade against safety guardrails",
        parameters={
            "action": "string",
            "ticker": "string",
            "quantity": "float",
            "price": "float",
            "portfolio_value": "float",
            "current_positions": "dict",
            "cash": "float",
        },
        function=risk.validate_order,
    ))

    return agent


def run_autonomous():
    """Execute the full autonomous decision loop."""
    logger.info("Starting autonomous decision loop")
    logger.info("Mode: %s", "PAPER" if ALPACA_PAPER else "LIVE")

    agent = build_agent()
    loop = DecisionLoop(agent)
    result = loop.run()

    # Save execution log
    log_path = save_canonical_log(result)
    logger.info("Execution log: %s", log_path)

    # Upload to Storacha if available
    if storacha_available():
        upload_result = upload_execution_log(result)
        if upload_result.get("success"):
            logger.info("Execution log uploaded to IPFS: %s", upload_result.get("cid"))
    else:
        logger.info("Storacha CLI not available — log stored locally only")

    # Enrich knowledge graph with this run's data
    if HAS_MEMORY:
        try:
            enrich_from_run(result)
            logger.info("Knowledge graph enriched from run")
        except Exception as e:
            logger.warning("KG enrichment failed (non-fatal): %s", e)

    # Generate plain-language narratives and daily reflection
    if HAS_NARRATOR:
        try:
            narrator = Narrator()

            # Narrate each decision
            for decision in result.get("decisions", []):
                narrative = narrator.narrate_decision(decision, regime=result.get("decisions", [{}])[0].get("result", "")[:100])
                if narrative:
                    logger.info("Narrative: %s", narrative[:100])

            # Daily reflection
            reflection = narrator.daily_reflection(
                decisions=result.get("decisions", []),
                portfolio_start={"portfolio_value": 100000},
                portfolio_end={"portfolio_value": 0},  # Will be filled by actual state
                regime_history=["NEUTRAL"],
            )
            if reflection:
                logger.info("Daily reflection generated")
                print(f"\n{'='*60}")
                print("DAILY REFLECTION")
                print(f"{'='*60}")
                print(reflection)
                print(f"{'='*60}\n")
        except Exception as e:
            logger.warning("Narration failed (non-fatal): %s", e)

    # Print summary
    print(f"\n{'='*60}")
    print("AUTONOMOUS EXECUTION COMPLETE")
    print(f"{'='*60}")
    print(f"Session:    {result.get('session_id', 'unknown')}")
    print(f"Phases:     {result.get('final_output', {}).get('phases_completed', 0)}/4")
    print(f"Decisions:  {len(result.get('decisions', []))}")
    print(f"Failures:   {len(result.get('failures', []))}")
    print(f"Log:        {log_path}")
    print(f"{'='*60}\n")

    return result


def run_query(query: str):
    """Run a single analysis query."""
    agent = build_agent()
    result = agent.run(query, verbose=True)

    # Save execution log
    if result.get("execution_log"):
        save_execution_log(result["execution_log"])

    return result


def run_scan():
    """Run a market opportunity scan."""
    from execution.scanner import run_full_scan
    results = run_full_scan()
    print(json.dumps(results, indent=2, default=str))
    return results


def main():
    parser = argparse.ArgumentParser(
        description="Sovereign",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py --autonomous           Run full decision loop
  python main.py --query "Analyze AAPL" Single analysis
  python main.py --scan                 Market opportunity scan
        """,
    )
    parser.add_argument("--autonomous", action="store_true", help="Run autonomous decision loop")
    parser.add_argument("--query", type=str, help="Run a single analysis query")
    parser.add_argument("--scan", action="store_true", help="Run market opportunity scan")
    parser.add_argument("--interview", action="store_true", help="Run investor profile intake interview")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.autonomous:
        run_autonomous()
    elif args.query:
        run_query(args.query)
    elif args.scan:
        run_scan()
    elif args.interview:
        from core.investor_profile import InvestorProfile
        profile = InvestorProfile()
        profile.run_interview()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
