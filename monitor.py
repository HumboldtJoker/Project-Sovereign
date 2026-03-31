#!/usr/bin/env python3
"""
Sovereign — Continuous Monitor

Tiered monitoring system:
  - Position checks:   every 30 seconds (stop-loss, circuit breaker, anomalies)
  - Strategy review:   every 60 minutes (thesis validation, regime shifts)
  - Opportunity scan:  every 4 hours (full discover → plan → execute → verify)

Usage:
    python monitor.py                   # Run all tiers
    python monitor.py --no-scan         # Skip opportunity scans (cron handles those)
    python monitor.py --dashboard       # Also start the dashboard on :8080
"""

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).parent))

from core.config import (
    ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_PAPER,
    DAILY_LOSS_LIMIT_PCT, DEFAULT_STOP_LOSS_PCT, VIX_ADAPTIVE_STOP,
    LOGS_DIR,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("monitor")

# ── Intervals (seconds) ─────────────────────────────────────────────────────
POSITION_CHECK_INTERVAL = 30
STRATEGY_REVIEW_INTERVAL = 60 * 60       # 1 hour
OPPORTUNITY_SCAN_INTERVAL = 60 * 60 * 4  # 4 hours

# ── State ────────────────────────────────────────────────────────────────────
monitor_state = {
    "running": True,
    "last_position_check": None,
    "last_strategy_review": None,
    "last_opportunity_scan": None,
    "position_checks": 0,
    "strategy_reviews": 0,
    "opportunity_scans": 0,
    "alerts": [],
    "portfolio_snapshots": [],
}


def check_positions():
    """
    Fast position check: stop-losses, circuit breakers, price anomalies.
    Runs every 30 seconds. No Claude API calls — pure data checks.
    """
    try:
        from alpaca.trading.client import TradingClient
        client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=ALPACA_PAPER)

        account = client.get_account()
        portfolio_value = float(account.portfolio_value)
        cash = float(account.cash)
        positions = client.get_all_positions()

        # Track portfolio value over time
        snapshot = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "portfolio_value": portfolio_value,
            "cash": cash,
            "position_count": len(positions),
        }
        monitor_state["portfolio_snapshots"].append(snapshot)

        # Keep last 1000 snapshots (~8 hours at 30s intervals)
        if len(monitor_state["portfolio_snapshots"]) > 1000:
            monitor_state["portfolio_snapshots"] = monitor_state["portfolio_snapshots"][-1000:]

        # Circuit breaker: check daily drawdown
        if monitor_state["portfolio_snapshots"]:
            day_start = None
            today = datetime.now(timezone.utc).date()
            for s in monitor_state["portfolio_snapshots"]:
                s_date = datetime.fromisoformat(s["timestamp"]).date()
                if s_date == today:
                    day_start = s
                    break

            if day_start:
                drawdown = (portfolio_value - day_start["portfolio_value"]) / day_start["portfolio_value"]
                if drawdown <= -DAILY_LOSS_LIMIT_PCT:
                    alert = {
                        "type": "CIRCUIT_BREAKER",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "detail": f"Daily drawdown {drawdown*100:.2f}% exceeds {DAILY_LOSS_LIMIT_PCT*100}% limit",
                        "portfolio_value": portfolio_value,
                    }
                    monitor_state["alerts"].append(alert)
                    logger.warning("CIRCUIT BREAKER: %s", alert["detail"])

        # Check each position for stop-loss
        for pos in positions:
            ticker = pos.symbol
            entry = float(pos.avg_entry_price)
            current = float(pos.current_price)
            pl_pct = float(pos.unrealized_plpc)

            # VIX-adaptive stop loss
            try:
                from analysis.macro import MacroAgent
                macro = MacroAgent()
                regime = macro.get_market_regime()
                vix = regime.get("indicators", {}).get("vix", {}).get("value", 20)
                if vix > 35:
                    stop_pct = VIX_ADAPTIVE_STOP["extreme"]
                elif vix > 25:
                    stop_pct = VIX_ADAPTIVE_STOP["elevated"]
                elif vix > 15:
                    stop_pct = VIX_ADAPTIVE_STOP["normal"]
                else:
                    stop_pct = VIX_ADAPTIVE_STOP["low"]
            except Exception:
                stop_pct = DEFAULT_STOP_LOSS_PCT

            if pl_pct <= -stop_pct:
                alert = {
                    "type": "STOP_LOSS",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "ticker": ticker,
                    "entry_price": entry,
                    "current_price": current,
                    "pl_pct": pl_pct,
                    "stop_threshold": stop_pct,
                    "detail": f"{ticker} hit stop-loss: {pl_pct*100:.2f}% (threshold: -{stop_pct*100}%)",
                }
                monitor_state["alerts"].append(alert)
                logger.warning("STOP-LOSS: %s", alert["detail"])

            # Price anomaly: >5% move since entry
            if abs(pl_pct) > 0.05:
                logger.info("Price alert: %s %+.2f%% since entry", ticker, pl_pct * 100)

        monitor_state["position_checks"] += 1
        monitor_state["last_position_check"] = datetime.now(timezone.utc).isoformat()

        if monitor_state["position_checks"] % 20 == 0:  # Log every ~10 minutes
            logger.info(
                "Position check #%d: $%,.2f portfolio, %d positions, %d alerts",
                monitor_state["position_checks"],
                portfolio_value,
                len(positions),
                len(monitor_state["alerts"]),
            )

    except Exception as e:
        logger.error("Position check failed: %s", e)


def strategy_review():
    """
    Hourly strategy review: regime changes, thesis validation.
    Uses Claude for reasoning but lighter than a full scan.
    """
    try:
        logger.info("Starting hourly strategy review...")

        from core.config import ANTHROPIC_API_KEY, CLAUDE_MODEL
        from analysis.macro import MacroAgent
        from analysis.technical import get_technical_indicators
        from alpaca.trading.client import TradingClient
        import anthropic

        # Get current regime
        macro = MacroAgent()
        regime = macro.get_market_regime()
        regime_name = regime.get("regime", "UNKNOWN")
        risk_score = regime.get("risk_score", 0)

        # Get current positions
        client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=ALPACA_PAPER)
        positions = client.get_all_positions()

        if not positions:
            logger.info("Strategy review: no positions to review")
            monitor_state["strategy_reviews"] += 1
            monitor_state["last_strategy_review"] = datetime.now(timezone.utc).isoformat()
            return

        # Quick technical check on each position
        position_data = []
        for pos in positions:
            tech = get_technical_indicators(pos.symbol, "1mo")
            signal = tech.get("overall_signal", {}).get("recommendation", "HOLD") if "error" not in tech else "UNKNOWN"
            position_data.append({
                "ticker": pos.symbol,
                "quantity": pos.qty,
                "entry": float(pos.avg_entry_price),
                "current": float(pos.current_price),
                "pl_pct": f"{float(pos.unrealized_plpc)*100:.2f}%",
                "signal": signal,
            })

        # Get KG context
        kg_context = ""
        try:
            from memory.market_context import build_market_context
            kg_context = build_market_context(regime_name)
        except Exception:
            pass

        # Quick Claude review
        claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        review_prompt = (
            f"QUICK STRATEGY REVIEW — answer in 3 sentences max.\n\n"
            f"Regime: {regime_name} (risk {risk_score}/10)\n"
            f"Positions: {json.dumps(position_data)}\n"
            f"{f'Historical context: {kg_context}' if kg_context else ''}\n\n"
            f"Should we HOLD, EXIT, or ADJUST any positions? Why?"
        )

        msg = claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=300,
            messages=[{"role": "user", "content": review_prompt}],
        )

        review = msg.content[0].text
        logger.info("Strategy review: %s", review[:200])

        # Log the review
        review_log = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "regime": regime_name,
            "risk_score": risk_score,
            "positions": position_data,
            "recommendation": review,
        }

        review_path = LOGS_DIR / "strategy_reviews.jsonl"
        with open(review_path, "a") as f:
            f.write(json.dumps(review_log, default=str) + "\n")

        # Generate plain-language narrative for retail investors
        try:
            from core.narrator import Narrator
            narrator = Narrator()
            plain = narrator.narrate_strategy_review(review_log)
            if plain:
                logger.info("Narrative: %s", plain[:150])
                review_log["narrative"] = plain
        except Exception:
            pass

        # Record in KG
        try:
            from memory.kg_engine import record_decision
            tickers = [p["ticker"] for p in position_data]
            record_decision(
                session_id="monitor",
                phase="strategy_review",
                action="hourly_review",
                tickers=tickers,
                reasoning=review[:500],
                regime=regime_name,
            )
        except Exception:
            pass

        monitor_state["strategy_reviews"] += 1
        monitor_state["last_strategy_review"] = datetime.now(timezone.utc).isoformat()

    except Exception as e:
        logger.error("Strategy review failed: %s", e)


def opportunity_scan():
    """
    Full autonomous decision loop: discover → plan → execute → verify.
    Runs every 4 hours.
    """
    try:
        logger.info("Starting opportunity scan (full autonomous loop)...")
        from main import run_autonomous
        result = run_autonomous()

        monitor_state["opportunity_scans"] += 1
        monitor_state["last_opportunity_scan"] = datetime.now(timezone.utc).isoformat()

        # Save timestamped log
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_dir = LOGS_DIR / "runs"
        run_dir.mkdir(exist_ok=True)
        log_path = run_dir / f"agent_log_{timestamp}.json"
        with open(log_path, "w") as f:
            json.dump(result, f, indent=2, default=str)

        logger.info("Opportunity scan complete. Log: %s", log_path)

    except Exception as e:
        logger.error("Opportunity scan failed: %s", e)


def monitor_loop(run_scans: bool = True):
    """Main monitoring loop with tiered intervals."""
    logger.info("="*60)
    logger.info("SOVEREIGN MARKET INTELLIGENCE AGENT — MONITOR")
    logger.info("="*60)
    logger.info("Position checks:   every %ds", POSITION_CHECK_INTERVAL)
    logger.info("Strategy reviews:  every %dm", STRATEGY_REVIEW_INTERVAL // 60)
    logger.info("Opportunity scans: every %dh%s", OPPORTUNITY_SCAN_INTERVAL // 3600,
                "" if run_scans else " (DISABLED)")
    logger.info("="*60)

    last_strategy = 0
    last_scan = 0

    def shutdown(sig, frame):
        logger.info("Shutdown signal received. Stopping monitor.")
        monitor_state["running"] = False

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    while monitor_state["running"]:
        now = time.time()

        # Position check (every 30s)
        check_positions()

        # Strategy review (every hour)
        if now - last_strategy >= STRATEGY_REVIEW_INTERVAL:
            threading.Thread(target=strategy_review, daemon=True).start()
            last_strategy = now

        # Opportunity scan (every 4 hours)
        if run_scans and now - last_scan >= OPPORTUNITY_SCAN_INTERVAL:
            threading.Thread(target=opportunity_scan, daemon=True).start()
            last_scan = now

        time.sleep(POSITION_CHECK_INTERVAL)

    logger.info("Monitor stopped.")


def main():
    parser = argparse.ArgumentParser(description="Sovereign Agent Monitor")
    parser.add_argument("--no-scan", action="store_true",
                        help="Disable opportunity scans (let cron handle them)")
    parser.add_argument("--dashboard", action="store_true",
                        help="Also start the dashboard on :8080")
    args = parser.parse_args()

    if args.dashboard:
        threading.Thread(target=_start_dashboard, daemon=True).start()

    monitor_loop(run_scans=not args.no_scan)


def _start_dashboard():
    """Start dashboard in background thread."""
    try:
        import uvicorn
        from dashboard.app import app
        uvicorn.run(app, host="0.0.0.0", port=8080, log_level="warning")
    except Exception as e:
        logger.error("Dashboard failed to start: %s", e)


if __name__ == "__main__":
    main()
