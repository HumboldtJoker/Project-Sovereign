"""
Investor Profile — Intake Interview + Preference Engine
========================================================
Before the agent makes any decisions, it needs to know WHO it's
investing for. Risk tolerance, time horizon, income needs, ethical
constraints — all of this shapes strategy.

In autonomous mode: uses the saved profile.
In interactive mode: conducts a plain-language intake interview.

Author: CC (Coalition Code)
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import anthropic

from core.config import ANTHROPIC_API_KEY, CLAUDE_MODEL, DATA_DIR

logger = logging.getLogger(__name__)

PROFILE_PATH = DATA_DIR / "investor_profile.json"

INTERVIEW_SYSTEM_PROMPT = """You are a friendly financial advisor conducting an intake interview.
Your job is to understand this person's investment needs in plain language.

Ask ONE question at a time. Keep it conversational. No jargon.
After 5-7 questions, you should have enough to build a profile.

Topics to cover:
1. What are you investing for? (retirement, house, education, growth, income)
2. When will you need this money? (months, years, decades)
3. How would you feel if your portfolio dropped 20% in a month?
4. Do you have other savings/income sources?
5. Any industries or companies you want to avoid? (ethical constraints)
6. How much time do you want to spend thinking about this? (hands-off vs active)

When you have enough, respond with PROFILE_COMPLETE and a JSON summary."""


class InvestorProfile:
    """Manages the investor's preferences and constraints."""

    # Default profile (used for our hackathon demo)
    DEFAULT = {
        "name": "Thomas",
        "risk_tolerance": 3,        # 1 (very conservative) to 5 (aggressive)
        "investment_horizon": "medium",  # short (<1yr), medium (1-5yr), long (5+yr)
        "goal": "growth with safety",
        "income_needs": False,
        "ethical_constraints": [],
        "hands_off": True,           # Prefers autonomous operation
        "max_drawdown_comfort": 0.10,  # Comfortable with 10% drawdown
        "notes": "Coalition member. Prefers defensive positioning in uncertain markets. Trust the safety system.",
        "created_at": "2026-03-28",
    }

    def __init__(self, profile: Optional[Dict] = None):
        if profile:
            self.profile = profile
        elif PROFILE_PATH.exists():
            with open(PROFILE_PATH) as f:
                self.profile = json.load(f)
            logger.info("Loaded investor profile: %s", self.profile.get("name", "Unknown"))
        else:
            self.profile = self.DEFAULT.copy()
            self.save()
            logger.info("Created default investor profile")

    def save(self):
        """Save profile to disk."""
        PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(PROFILE_PATH, "w") as f:
            json.dump(self.profile, f, indent=2, default=str)

    @property
    def risk_tolerance(self) -> int:
        return self.profile.get("risk_tolerance", 3)

    @property
    def horizon(self) -> str:
        return self.profile.get("investment_horizon", "medium")

    @property
    def max_drawdown(self) -> float:
        return self.profile.get("max_drawdown_comfort", 0.10)

    def get_allocation_guidance(self, regime: str) -> Dict:
        """
        Generate allocation guidance based on profile + regime.

        Returns target allocation ranges.
        """
        rt = self.risk_tolerance

        # Base equity allocation by risk tolerance
        base_equity = {1: 0.20, 2: 0.35, 3: 0.50, 4: 0.65, 5: 0.80}
        equity_target = base_equity.get(rt, 0.50)

        # Regime adjustment
        regime_adj = {
            "BULLISH": 1.1,
            "NEUTRAL": 1.0,
            "CAUTIOUS": 0.7,
            "BEARISH": 0.4,
            "CRITICAL": 0.0,
        }
        modifier = regime_adj.get(regime, 1.0)
        equity_target = min(0.90, equity_target * modifier)

        # Horizon adjustment
        if self.horizon == "short":
            equity_target *= 0.6
        elif self.horizon == "long":
            equity_target = min(0.90, equity_target * 1.2)

        cash_target = 1.0 - equity_target

        # Min positions based on risk tolerance
        min_positions = max(3, 2 + rt)  # RT 1→3, RT 3→5, RT 5→7

        return {
            "equity_target_pct": round(equity_target * 100),
            "cash_target_pct": round(cash_target * 100),
            "min_positions": min_positions,
            "max_single_position_pct": 30 if rt >= 4 else 20 if rt >= 2 else 15,
            "income_needed": self.profile.get("income_needs", False),
            "ethical_exclusions": self.profile.get("ethical_constraints", []),
        }

    def get_prompt_context(self, regime: str = "NEUTRAL") -> str:
        """Generate a context string for the agent's system prompt."""
        guidance = self.get_allocation_guidance(regime)

        lines = [
            f"INVESTOR PROFILE: {self.profile.get('name', 'User')}",
            f"  Risk tolerance: {self.risk_tolerance}/5",
            f"  Horizon: {self.horizon}",
            f"  Goal: {self.profile.get('goal', 'growth')}",
            f"  Max comfortable drawdown: {self.max_drawdown*100:.0f}%",
            f"  Hands-off preference: {'yes' if self.profile.get('hands_off') else 'no'}",
            f"",
            f"ALLOCATION GUIDANCE for {regime} regime:",
            f"  Target equity: {guidance['equity_target_pct']}%",
            f"  Target cash: {guidance['cash_target_pct']}%",
            f"  Minimum positions: {guidance['min_positions']}",
            f"  Max single position: {guidance['max_single_position_pct']}%",
        ]

        if guidance["ethical_exclusions"]:
            lines.append(f"  EXCLUDE: {', '.join(guidance['ethical_exclusions'])}")

        if self.profile.get("notes"):
            lines.append(f"  Notes: {self.profile['notes']}")

        return "\n".join(lines)

    def run_interview(self) -> Dict:
        """
        Conduct an interactive intake interview via the terminal.
        Returns the completed profile.
        """
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        messages = []

        print("\n" + "=" * 60)
        print("INVESTOR PROFILE — INTAKE INTERVIEW")
        print("=" * 60)
        print("Let's figure out the right investment approach for you.")
        print("Type 'skip' to use the default profile.\n")

        # Initial prompt
        messages.append({
            "role": "user",
            "content": "Hi, I'd like to set up my investment profile."
        })

        for _ in range(10):  # Max 10 turns
            response = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=300,
                system=INTERVIEW_SYSTEM_PROMPT,
                messages=messages,
            )

            assistant_text = response.content[0].text
            messages.append({"role": "assistant", "content": assistant_text})

            if "PROFILE_COMPLETE" in assistant_text:
                # Extract JSON from response
                try:
                    import re
                    json_match = re.search(r'\{[\s\S]*\}', assistant_text)
                    if json_match:
                        profile_data = json.loads(json_match.group())
                        self.profile.update(profile_data)
                        self.profile["created_at"] = datetime.now(timezone.utc).isoformat()
                        self.save()
                        print("\nProfile saved!")
                        return self.profile
                except Exception as e:
                    logger.warning("Failed to parse profile: %s", e)
                break

            print(f"Advisor: {assistant_text}\n")
            user_input = input("You: ").strip()

            if user_input.lower() == "skip":
                print("Using default profile.")
                return self.profile

            messages.append({"role": "user", "content": user_input})

        return self.profile
