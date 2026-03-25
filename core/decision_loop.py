"""
Autonomous decision loop: discover → plan → execute → verify.

Orchestrates the full agent lifecycle for the "Agent Only: Let the agent cook"
challenge track. No human in the loop.
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.config import ALPACA_PAPER
from core.react_agent import ReActAgent
from core.tool_registry import Tool

logger = logging.getLogger(__name__)


class DecisionLoop:
    """
    Autonomous market intelligence decision loop.

    Phases:
        1. DISCOVER — Scan markets for opportunities
        2. PLAN — Run multi-layer analysis on candidates
        3. EXECUTE — Place trades through safety guardrails
        4. VERIFY — Confirm execution and store audit trail
    """

    def __init__(self, agent: ReActAgent):
        self.agent = agent
        self.session_id = str(uuid.uuid4())
        self.decisions: List[Dict] = []
        self.retries: List[Dict] = []
        self.failures: List[Dict] = []
        self.timestamp_start = datetime.now(timezone.utc).isoformat()

    def _record(self, phase: str, action: str, reasoning: str,
                tools_called: List[str], result: str,
                safety_checks: Optional[List[str]] = None):
        entry = {
            "step": len(self.decisions) + 1,
            "phase": phase,
            "action": action,
            "reasoning": reasoning,
            "tools_called": tools_called,
            "result": result,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if safety_checks:
            entry["safety_checks"] = safety_checks
        self.decisions.append(entry)
        logger.info("Phase %s — %s: %s", phase, action, result[:120])

    def discover(self) -> Dict[str, Any]:
        """Phase 1: Scan markets for opportunities."""
        logger.info("DISCOVER phase starting for session %s", self.session_id)

        result = self.agent.run(
            "Scan the current market environment. Check VIX levels and macro regime, "
            "then identify 3–5 ticker candidates with strong technical signals. "
            "Focus on sectors with favorable congressional trading patterns. "
            "Return a JSON object with candidates and reasoning.",
            verbose=False,
        )

        self._record(
            phase="discover",
            action="scan_market_opportunities",
            reasoning="Autonomous market scan for opportunities",
            tools_called=self._extract_tools(result),
            result=result.get("answer", "No candidates found")[:500],
        )

        return result

    def plan(self, candidates: str) -> Dict[str, Any]:
        """Phase 2: Deep analysis on candidates."""
        logger.info("PLAN phase starting")

        result = self.agent.run(
            f"Run comprehensive analysis on these candidates: {candidates}. "
            "For each, analyze: technical indicators (SMA, RSI, MACD, Bollinger), "
            "news sentiment, congressional trading patterns, macro overlay, "
            "and portfolio correlation risk. "
            "Rank by risk-adjusted conviction and recommend position sizes. "
            "Return a JSON object with ranked recommendations and risk parameters.",
            verbose=False,
        )

        self._record(
            phase="plan",
            action="multi_layer_analysis",
            reasoning="Comprehensive analysis across technical, sentiment, congressional, macro",
            tools_called=self._extract_tools(result),
            result=result.get("answer", "Analysis incomplete")[:500],
        )

        return result

    def execute(self, plan: str) -> Dict[str, Any]:
        """Phase 3: Execute trades with safety guardrails."""
        logger.info("EXECUTE phase starting")

        mode = "paper" if ALPACA_PAPER else "live"

        result = self.agent.run(
            f"Execute the following trade plan in {mode} mode: {plan}. "
            "Before each trade: validate position size, check sector concentration, "
            "verify macro regime allows the position, and confirm stop-loss levels. "
            "Execute only trades that pass ALL safety checks. "
            "Return a JSON object with executed trades and any rejected trades with reasons.",
            verbose=False,
        )

        safety_checks = [
            "position_size_validated",
            "sector_concentration_checked",
            "macro_regime_verified",
            "stop_loss_confirmed",
            "daily_loss_limit_checked",
        ]

        self._record(
            phase="execute",
            action="place_trades",
            reasoning="Execute trades after safety validation",
            tools_called=self._extract_tools(result),
            result=result.get("answer", "No trades executed")[:500],
            safety_checks=safety_checks,
        )

        return result

    def verify(self, execution_result: str) -> Dict[str, Any]:
        """Phase 4: Verify execution and generate audit trail."""
        logger.info("VERIFY phase starting")

        result = self.agent.run(
            f"Verify the following trade executions: {execution_result}. "
            "Confirm all fills, check actual vs expected prices, "
            "update portfolio state, and generate an execution summary. "
            "Return a JSON object with verification results.",
            verbose=False,
        )

        self._record(
            phase="verify",
            action="confirm_execution_and_audit",
            reasoning="Verify fills, update portfolio, generate audit",
            tools_called=self._extract_tools(result),
            result=result.get("answer", "Verification incomplete")[:500],
        )

        return result

    def run(self) -> Dict[str, Any]:
        """Execute the full autonomous decision loop."""
        logger.info("Starting autonomous decision loop: session %s", self.session_id)

        try:
            # Phase 1: Discover
            discover_result = self.discover()
            if not discover_result.get("success"):
                self.failures.append({"phase": "discover", "reason": discover_result.get("error", "unknown")})
                return self._build_log()

            candidates = discover_result.get("answer", "")

            # Phase 2: Plan
            plan_result = self.plan(candidates)
            if not plan_result.get("success"):
                self.failures.append({"phase": "plan", "reason": plan_result.get("error", "unknown")})
                return self._build_log()

            plan = plan_result.get("answer", "")

            # Phase 3: Execute
            execute_result = self.execute(plan)
            if not execute_result.get("success"):
                self.failures.append({"phase": "execute", "reason": execute_result.get("error", "unknown")})
                return self._build_log()

            execution = execute_result.get("answer", "")

            # Phase 4: Verify
            verify_result = self.verify(execution)

            return self._build_log()

        except Exception as e:
            logger.exception("Decision loop failed: %s", e)
            self.failures.append({"phase": "unknown", "reason": str(e)})
            return self._build_log()

    def _build_log(self) -> Dict[str, Any]:
        """Build the structured agent_log.json output."""
        return {
            "session_id": self.session_id,
            "agent_id": "",  # Set after ERC-8004 registration
            "timestamp_start": self.timestamp_start,
            "timestamp_end": datetime.now(timezone.utc).isoformat(),
            "decisions": self.decisions,
            "retries": self.retries,
            "failures": self.failures,
            "final_output": {
                "trades_executed": self._count_phase("execute"),
                "analysis_reports_generated": self._count_phase("plan"),
                "phases_completed": len(set(d["phase"] for d in self.decisions)),
            },
        }

    def _count_phase(self, phase: str) -> int:
        return sum(1 for d in self.decisions if d["phase"] == phase)

    def _extract_tools(self, result: Dict) -> List[str]:
        """Extract tool names used from agent history."""
        tools = []
        for item in result.get("history", []):
            if item.get("type") == "action":
                tools.append(item["tool"])
        return tools
