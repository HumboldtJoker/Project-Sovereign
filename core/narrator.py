"""
Market Narrative Generator + Self-Reflection Journal
=====================================================
Transforms raw agent decisions into human-readable market commentary
that's accessible to retail investors, not just quants.

Two modes:
  1. Decision narratives: real-time plain-language explanation of each trade
  2. Daily reflections: end-of-day journal reviewing decisions vs outcomes

Author: CC (Coalition Code)
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import anthropic

from core.config import ANTHROPIC_API_KEY, CLAUDE_MODEL, LOGS_DIR

logger = logging.getLogger(__name__)


NARRATIVE_SYSTEM_PROMPT = """You are a friendly, clear market analyst writing for regular people —
not Wall Street professionals. Your reader is someone who has a 401k and reads the news,
but doesn't know what RSI or MACD means.

Your job is to explain investment decisions in plain language:
- No jargon without explanation. If you mention VIX, say "the VIX (a fear gauge for the market)"
- Use analogies. "Think of it like..." comparisons to everyday life
- Be honest about uncertainty. "We think..." not "We know..."
- Explain the WHY, not just the WHAT
- Keep it conversational but trustworthy
- When referencing famous investors, briefly explain who they are and why their advice matters
- Always mention risk and what could go wrong — never sound like a sure thing

Length: 2-4 paragraphs. Readable in under 60 seconds."""


REFLECTION_SYSTEM_PROMPT = """You are a thoughtful investor writing in your trading journal at the
end of the day. You're honest about mistakes, curious about what surprised you, and focused on
what you'll do differently next time.

Write for yourself — but in a way that anyone could read and learn from:
- What did you decide today and why?
- What happened — did the market agree with you?
- What surprised you?
- What would Buffett/Dalio/Marks say about your decisions?
- What will you do differently next time?
- One lesson learned, stated simply

Keep it genuine. This isn't a report — it's reflection. 2-3 paragraphs.
End with a one-sentence takeaway that a beginner investor could pin to their wall."""


class Narrator:
    """Generates plain-language market narratives and reflections."""

    def __init__(self):
        self.client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        self.narratives: List[Dict] = []
        self.reflections: List[Dict] = []

    def narrate_decision(self, decision: Dict, portfolio_state: Dict = None,
                         regime: str = "", kg_context: str = "") -> str:
        """
        Generate a plain-language narrative for a trading decision.

        Args:
            decision: Dict from the execution log (phase, action, tools, result)
            portfolio_state: Current portfolio (cash, positions, value)
            regime: Current market regime
            kg_context: Relevant knowledge graph context (investor wisdom, history)

        Returns:
            Human-readable narrative string
        """
        prompt = f"""Explain this investment decision in plain language for a regular person:

WHAT HAPPENED:
Phase: {decision.get('phase', 'unknown')}
Action: {decision.get('action', 'unknown')}
Tools used: {decision.get('tools_called', [])}
Result: {str(decision.get('result', ''))[:500]}

MARKET CONDITIONS:
Regime: {regime}

PORTFOLIO:
{json.dumps(portfolio_state, indent=2, default=str)[:300] if portfolio_state else 'Not available'}

RELEVANT WISDOM:
{kg_context[:400] if kg_context else 'None available'}

Write the explanation now. Remember: no jargon without definition, use analogies,
be honest about risk."""

        try:
            msg = self.client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=500,
                system=NARRATIVE_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            narrative = msg.content[0].text

            entry = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "phase": decision.get("phase"),
                "narrative": narrative,
            }
            self.narratives.append(entry)

            # Save to log
            self._append_to_log("narratives.jsonl", entry)

            return narrative

        except Exception as e:
            logger.error("Narrative generation failed: %s", e)
            return ""

    def daily_reflection(self, decisions: List[Dict], portfolio_start: Dict,
                         portfolio_end: Dict, regime_history: List[str] = None) -> str:
        """
        Generate an end-of-day reflection journal entry.

        Args:
            decisions: All decisions made today
            portfolio_start: Portfolio state at start of day
            portfolio_end: Portfolio state at end of day
            regime_history: Regime changes during the day
        """
        # Calculate day's P&L
        start_value = portfolio_start.get("portfolio_value", 0) if portfolio_start else 0
        end_value = portfolio_end.get("portfolio_value", 0) if portfolio_end else 0
        day_pl = end_value - start_value
        day_pct = (day_pl / start_value * 100) if start_value else 0

        decisions_summary = []
        for d in decisions:
            decisions_summary.append(
                f"- Phase: {d.get('phase')}, Action: {d.get('action')}, "
                f"Result: {str(d.get('result', ''))[:150]}"
            )

        prompt = f"""Write your end-of-day trading journal entry.

TODAY'S DECISIONS:
{chr(10).join(decisions_summary) if decisions_summary else 'No trades made today.'}

PORTFOLIO CHANGE:
Start: ${start_value:,.2f}
End: ${end_value:,.2f}
Day P&L: ${day_pl:+,.2f} ({day_pct:+.2f}%)

REGIME(S) TODAY: {', '.join(regime_history) if regime_history else 'Unknown'}

Reflect honestly. What went right, what went wrong, what did you learn?
End with one sentence a beginner could learn from."""

        try:
            msg = self.client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=600,
                system=REFLECTION_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            reflection = msg.content[0].text

            entry = {
                "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "day_pl": day_pl,
                "day_pct": day_pct,
                "decisions_count": len(decisions),
                "reflection": reflection,
            }
            self.reflections.append(entry)

            # Save to log
            self._append_to_log("reflections.jsonl", entry)

            # Feed back into KG
            try:
                from memory.kg_engine import record_event
                record_event(
                    event_text=f"SELF-REFLECTION ({entry['date']}): {reflection[:300]}",
                    event_type="reflection",
                    entities=[],
                    impact_score=0.5,
                    regime=regime_history[-1] if regime_history else "UNKNOWN",
                )
            except Exception:
                pass

            return reflection

        except Exception as e:
            logger.error("Reflection generation failed: %s", e)
            return ""

    def narrate_strategy_review(self, review_data: Dict) -> str:
        """
        Generate a plain-language explanation of an hourly strategy review.
        Designed for the dashboard or a notification to the user.
        """
        prompt = f"""A strategy review just happened. Explain it simply:

REGIME: {review_data.get('regime', '?')} (risk level: {review_data.get('risk_score', '?')}/10)
POSITIONS: {json.dumps(review_data.get('positions', []), default=str)[:300]}
RECOMMENDATION: {review_data.get('recommendation', 'No recommendation')[:300]}

Explain in 2-3 sentences what this means for someone checking their portfolio.
Use plain language. If the recommendation is "hold" explain why doing nothing is a strategy,
not laziness."""

        try:
            msg = self.client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=200,
                system=NARRATIVE_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            return msg.content[0].text
        except Exception as e:
            logger.error("Strategy narrative failed: %s", e)
            return ""

    def _append_to_log(self, filename: str, entry: Dict):
        """Append an entry to a JSONL log file."""
        log_path = LOGS_DIR / filename
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
