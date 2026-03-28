"""
ReAct (Reasoning + Acting) Agent for autonomous market intelligence.

Implements the core reasoning loop: Thought → Action → Observation → Final Answer.
Designed for fully autonomous operation with structured execution logging.
"""

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import anthropic

from core.config import ANTHROPIC_API_KEY, CLAUDE_MODEL, MAX_ITERATIONS
from core.tool_registry import ToolRegistry

logger = logging.getLogger(__name__)


class ReActAgent:
    """
    Autonomous ReAct agent for investment research and trade execution.

    Uses Claude for reasoning through market analysis by:
    1. Breaking down the task into steps (Reasoning)
    2. Using tools to gather data and execute actions (Acting)
    3. Synthesizing information into decisions
    """

    def __init__(
        self,
        api_key: str = "",
        model: str = "",
        max_iterations: int = 0,
    ):
        self.api_key = api_key or ANTHROPIC_API_KEY
        self.model = model or CLAUDE_MODEL
        self.max_iterations = max_iterations or MAX_ITERATIONS
        self.client = anthropic.Anthropic(api_key=self.api_key)
        self.tools = ToolRegistry()
        self.history: List[Dict] = []
        self.execution_log: List[Dict] = []

    def _build_system_prompt(self) -> str:
        # Build market context from KG if available
        market_context = ""
        try:
            from memory.market_context import build_market_context
            market_context = build_market_context()
            if market_context:
                market_context = f"\nMARKET MEMORY (from knowledge graph):\n{market_context}\n"
        except Exception:
            pass  # KG not available — agent runs without historical context

        # Build investor profile context
        investor_context = ""
        try:
            from core.investor_profile import InvestorProfile
            profile = InvestorProfile()
            investor_context = f"\n{profile.get_prompt_context()}\n"
        except Exception:
            pass

        return f"""You are an autonomous market intelligence agent using ReAct methodology.

Your mission is to provide thorough, data-driven investment analysis by:
1. Breaking down complex research questions into steps
2. Using available tools to gather real market data
3. Reasoning through information systematically
4. Providing clear, actionable recommendations
{investor_context}{market_context}
RULES:
- Always show reasoning before taking actions
- Use tools for real data — never fabricate numbers
- Be efficient: gather the data you need, then conclude. Do NOT exceed 8 tool calls.
- You MUST reach a FINAL_ANSWER. If uncertain, state your uncertainty in the answer.
- Provide specific recommendations with clear rationale
- Every decision must pass through safety guardrails before execution
- Use MARKET MEMORY above to inform your analysis — learn from past regimes and outcomes

AVAILABLE TOOLS:
{self.tools.get_descriptions()}

OUTPUT FORMAT:
For each step, use this exact format:

Thought: [Your reasoning about what to do next]
Action: [tool_name]
Action Input: [tool parameters as JSON]

After receiving observations:

Thought: [Your analysis of the observation]
Action: [next tool_name or FINAL_ANSWER]
Action Input: [parameters or your final recommendation]

When complete:
Action: FINAL_ANSWER
Action Input: [Your complete analysis and recommendation as JSON]
"""

    def _parse_response(self, response: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """Extract thought, action, and action input from Claude's response."""
        thought_match = re.search(
            r"Thought:\s*(.+?)(?=\nAction:|$)", response, re.DOTALL | re.IGNORECASE
        )
        action_match = re.search(r"Action:\s*(\w+)", response, re.IGNORECASE)
        action_input_match = re.search(
            r"Action Input:\s*(.+?)(?=\n\n|$)", response, re.DOTALL | re.IGNORECASE
        )

        thought = thought_match.group(1).strip() if thought_match else None
        action = action_match.group(1).strip() if action_match else None
        action_input = action_input_match.group(1).strip() if action_input_match else None

        return thought, action, action_input

    def _execute_tool(self, tool_name: str, tool_input: str) -> str:
        """Execute a tool and return the observation string."""
        tool = self.tools.get(tool_name)
        if not tool:
            available = ", ".join(self.tools.tool_names())
            return f"ERROR: Tool '{tool_name}' not found. Available: {available}"

        try:
            params = json.loads(tool_input)
            result = tool.execute(**params)
            if result["success"]:
                return json.dumps(result["data"], indent=2, default=str)
            return f"ERROR: {result['error']}"
        except json.JSONDecodeError as e:
            return f"ERROR: Invalid JSON in Action Input: {e}"
        except Exception as e:
            return f"ERROR: Tool execution failed: {e}"

    def _format_history(self) -> str:
        """Format conversation history for the next iteration's prompt."""
        formatted = []
        for item in self.history:
            if item["type"] == "thought":
                formatted.append(f"\nIteration {item['iteration'] + 1}:")
                formatted.append(f"Thought: {item['content']}")
            elif item["type"] == "action":
                formatted.append(f"Action: {item['tool']}")
                formatted.append(f"Action Input: {item['input']}")
            elif item["type"] == "observation":
                formatted.append(f"Observation: {item['content']}")
        return "\n".join(formatted)

    def _log_decision(self, step: int, phase: str, action: str, reasoning: str,
                      tools_called: List[str], result: str,
                      safety_checks: Optional[List[str]] = None):
        """Record a decision step for the structured execution log."""
        entry = {
            "step": step,
            "phase": phase,
            "action": action,
            "reasoning": reasoning,
            "tools_called": tools_called,
            "result": result,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if safety_checks:
            entry["safety_checks"] = safety_checks
        self.execution_log.append(entry)

    def run(self, user_query: str, verbose: bool = False) -> Dict[str, Any]:
        """
        Execute the ReAct loop to answer a query.

        Returns dict with success, answer, iterations, history, and execution_log.
        """
        session_id = str(uuid.uuid4())
        self.history = []
        self.execution_log = []
        timestamp_start = datetime.now(timezone.utc).isoformat()

        logger.info("Starting ReAct session %s: %s", session_id, user_query[:80])

        for iteration in range(self.max_iterations):
            system_prompt = self._build_system_prompt()

            if iteration == 0:
                user_prompt = f"USER QUERY: {user_query}\n\nBegin your research:"
            else:
                user_prompt = self._format_history() + "\n\nContinue your research:"

            if verbose:
                print(f"\n--- Iteration {iteration + 1} ---")

            message = self.client.messages.create(
                model=self.model,
                max_tokens=4000,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )

            response = message.content[0].text
            thought, action, action_input = self._parse_response(response)

            if verbose and thought:
                print(f"Thought: {thought}")

            if thought:
                self.history.append({
                    "type": "thought",
                    "content": thought,
                    "iteration": iteration,
                })

            # Final answer
            if action and action.upper() == "FINAL_ANSWER":
                self._log_decision(
                    step=iteration + 1,
                    phase="conclude",
                    action="final_answer",
                    reasoning=thought or "",
                    tools_called=[],
                    result=action_input or "",
                )

                if verbose:
                    print(f"\nFINAL ANSWER:\n{action_input}\n")

                return {
                    "success": True,
                    "answer": action_input,
                    "session_id": session_id,
                    "iterations": iteration + 1,
                    "history": self.history,
                    "execution_log": {
                        "session_id": session_id,
                        "timestamp_start": timestamp_start,
                        "timestamp_end": datetime.now(timezone.utc).isoformat(),
                        "decisions": self.execution_log,
                        "retries": [],
                        "failures": [],
                        "final_output": {
                            "answer": action_input,
                            "iterations": iteration + 1,
                        },
                    },
                }

            # Execute tool
            if action and action_input:
                if verbose:
                    print(f"Action: {action}")
                    print(f"Action Input: {action_input}")

                self.history.append({
                    "type": "action",
                    "tool": action,
                    "input": action_input,
                    "iteration": iteration,
                })

                observation = self._execute_tool(action, action_input)

                self._log_decision(
                    step=iteration + 1,
                    phase="discover" if iteration < 2 else "analyze",
                    action=f"call_{action}",
                    reasoning=thought or "",
                    tools_called=[action],
                    result=observation[:200],
                )

                if verbose:
                    preview = observation[:500] + "..." if len(observation) > 500 else observation
                    print(f"Observation: {preview}")

                self.history.append({
                    "type": "observation",
                    "content": observation,
                    "iteration": iteration,
                })
            else:
                logger.warning("Could not parse action from response at iteration %d", iteration)
                break

        # Max iterations reached
        return {
            "success": False,
            "error": f"Max iterations ({self.max_iterations}) reached without final answer",
            "session_id": session_id,
            "iterations": self.max_iterations,
            "history": self.history,
            "execution_log": {
                "session_id": session_id,
                "timestamp_start": timestamp_start,
                "timestamp_end": datetime.now(timezone.utc).isoformat(),
                "decisions": self.execution_log,
                "retries": [],
                "failures": [{"reason": "max_iterations_reached"}],
                "final_output": None,
            },
        }
