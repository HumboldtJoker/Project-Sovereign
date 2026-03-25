"""
Strategy Trigger for PLGenesis Market Agent.

Allows Python execution monitor to trigger strategic reviews via Claude API
using the MCP (Model Context Protocol) tools for market analysis.

This module provides a bridge between the autonomous Python monitoring system
and the Strategy Agent (Claude with MCP tools).
"""

import json
import logging
from typing import Dict, Any, Optional
from datetime import datetime

try:
    from anthropic import Anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False

from core.config import ANTHROPIC_API_KEY, CLAUDE_MODEL

logger = logging.getLogger(__name__)


class StrategyTrigger:
    """
    Wrapper for triggering strategic reviews via Claude API with MCP tools.
    """

    def __init__(self, api_key: Optional[str] = None):
        """
        Initialize strategy trigger.

        Args:
            api_key: Anthropic API key (defaults to ANTHROPIC_API_KEY from config)
        """
        if not ANTHROPIC_AVAILABLE:
            raise ImportError("anthropic package not installed. Run: pip install anthropic>=0.39.0")

        # Get API key
        self.api_key = api_key or ANTHROPIC_API_KEY
        if not self.api_key:
            raise ValueError("ANTHROPIC_API_KEY not found in environment variables")

        # Initialize Anthropic client
        self.client = Anthropic(api_key=self.api_key)

        # Model configuration
        self.model = CLAUDE_MODEL
        self.max_tokens = 4096

    def trigger_strategic_review(
        self,
        reason: str,
        context: Dict[str, Any],
        include_technical: bool = True,
        include_macro: bool = True,
    ) -> Dict[str, Any]:
        """
        Trigger a strategic review via Claude with MCP tools.

        Args:
            reason: Why this review is being triggered (e.g., "VIX crossed above 20")
            context: Current portfolio context (positions, cash, P&L, etc.)
            include_technical: Whether to request technical analysis
            include_macro: Whether to request macro analysis

        Returns:
            Dict containing:
                - success: bool
                - analysis: str (Claude's strategic review)
                - recommendations: list of recommended actions
                - timestamp: when review was conducted
                - error: str (if failed)
        """
        try:
            # Format the strategic review prompt
            prompt = self._format_strategic_prompt(reason, context, include_technical, include_macro)

            # Call Claude API with MCP tools enabled
            response = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                messages=[
                    {
                        "role": "user",
                        "content": prompt,
                    }
                ],
            )

            # Extract response
            analysis_text = response.content[0].text if response.content else "No response"

            # Parse recommendations (look for action items in response)
            recommendations = self._extract_recommendations(analysis_text)

            return {
                "success": True,
                "analysis": analysis_text,
                "recommendations": recommendations,
                "timestamp": datetime.now().isoformat(),
                "tokens_used": response.usage.input_tokens + response.usage.output_tokens,
                "cost": self._calculate_cost(response.usage.input_tokens, response.usage.output_tokens),
            }

        except Exception as e:
            logger.error("Strategic review failed: %s", e)
            return {
                "success": False,
                "error": str(e),
                "timestamp": datetime.now().isoformat(),
            }

    def _format_strategic_prompt(
        self,
        reason: str,
        context: Dict[str, Any],
        include_technical: bool,
        include_macro: bool,
    ) -> str:
        """
        Format the strategic review prompt for Claude.

        Args:
            reason: Trigger reason
            context: Portfolio context
            include_technical: Request technical analysis
            include_macro: Request macro analysis

        Returns:
            Formatted prompt string
        """
        # Format portfolio positions
        positions_text = "\n".join([
            f"  - {pos['ticker']}: {pos['quantity']:.4f} shares @ ${pos['avg_cost']:.2f} "
            f"(current: ${pos['current_price']:.2f}, P&L: {((pos['current_price'] - pos['avg_cost']) / pos['avg_cost'] * 100):+.2f}%)"
            for pos in context.get('positions', [])
        ])

        prompt = f"""STRATEGIC REVIEW TRIGGERED: {reason}

**Current Portfolio Status:**
- Total Value: ${context.get('total_value', 0):.2f}
- Cash: ${context.get('cash', 0):.2f}
- Total P&L: ${context.get('total_unrealized_pl', 0):.2f} ({context.get('total_return', 0):.2f}%)

**Positions:**
{positions_text}

**Review Request:**
Please conduct a strategic review of the current portfolio given the trigger: {reason}

"""

        if include_technical:
            prompt += """1. **Technical Analysis**: Use the technical_analysis MCP tool to check RSI, MACD, and momentum indicators for each position.

"""

        if include_macro:
            prompt += """2. **Macro Analysis**: Use the macro_analysis MCP tool to assess current market regime (risk-on vs risk-off).

"""

        prompt += """3. **Portfolio Analysis**: Use portfolio_analysis to check correlation and diversification.

4. **Recommendations**: Provide specific, actionable recommendations:
   - Should any positions be reduced or closed?
   - Should we increase cash reserves?
   - Are stop-losses appropriate for current volatility?
   - Any rebalancing needed?

Format your response with clear sections:
- ANALYSIS
- KEY FINDINGS
- RECOMMENDATIONS (numbered list)
"""

        return prompt

    def _extract_recommendations(self, analysis_text: str) -> list:
        """
        Extract actionable recommendations from analysis text.

        Args:
            analysis_text: Full analysis response

        Returns:
            List of recommendation strings
        """
        recommendations = []

        # Look for RECOMMENDATIONS section
        if "RECOMMENDATIONS" in analysis_text:
            parts = analysis_text.split("RECOMMENDATIONS")
            if len(parts) > 1:
                rec_section = parts[1]

                # Extract numbered items
                lines = rec_section.split('\n')
                for line in lines:
                    line = line.strip()
                    # Match patterns like "1.", "1)", "-", "*"
                    if line and (line[0].isdigit() or line.startswith('-') or line.startswith('*')):
                        # Clean up the line
                        cleaned = line.lstrip('0123456789.-*) ')
                        if cleaned:
                            recommendations.append(cleaned)

        return recommendations

    def _calculate_cost(self, input_tokens: int, output_tokens: int) -> float:
        """
        Calculate API cost based on token usage.

        Claude Sonnet 4.5 pricing:
        - Input: $3 per million tokens
        - Output: $15 per million tokens

        Args:
            input_tokens: Number of input tokens
            output_tokens: Number of output tokens

        Returns:
            Total cost in USD
        """
        input_cost = (input_tokens / 1_000_000) * 3.0
        output_cost = (output_tokens / 1_000_000) * 15.0
        return input_cost + output_cost

    def trigger_vix_review(
        self,
        vix_level: float,
        previous_vix: float,
        regime: str,
        previous_regime: str,
        portfolio_context: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Specialized trigger for VIX-based strategic reviews.

        Args:
            vix_level: Current VIX level
            previous_vix: Previous VIX level
            regime: Current VIX regime (CALM/NORMAL/ELEVATED/HIGH)
            previous_regime: Previous regime
            portfolio_context: Current portfolio state

        Returns:
            Strategic review result
        """
        # Format reason
        direction = "increased" if vix_level > previous_vix else "decreased"
        change_pct = abs((vix_level - previous_vix) / previous_vix * 100)

        reason = (
            f"VIX regime change: {previous_regime} -> {regime}\n"
            f"VIX {direction} from {previous_vix:.2f} to {vix_level:.2f} ({change_pct:.1f}% change)"
        )

        # Add VIX context to portfolio context
        portfolio_context['vix'] = {
            'current': vix_level,
            'previous': previous_vix,
            'regime': regime,
            'previous_regime': previous_regime,
        }

        # Trigger review with macro emphasis
        return self.trigger_strategic_review(
            reason=reason,
            context=portfolio_context,
            include_technical=True,
            include_macro=True,
        )


# Convenience function for direct usage
def trigger_strategic_review(reason: str, context: Dict[str, Any]) -> Dict[str, Any]:
    """
    Convenience function to trigger strategic review without creating wrapper instance.

    Args:
        reason: Why review is being triggered
        context: Portfolio context

    Returns:
        Strategic review result
    """
    trigger = StrategyTrigger()
    return trigger.trigger_strategic_review(reason, context)
