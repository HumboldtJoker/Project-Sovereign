"""
Structured execution logger for agent_log.json generation.

Produces the structured logs required by the Agent Only and Agents With Receipts
challenge tracks.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from core.config import ERC8004_AGENT_ID, LOGS_DIR

logger = logging.getLogger(__name__)


def save_execution_log(log_data: Dict[str, Any], filename: Optional[str] = None) -> Path:
    """
    Save structured execution log to disk.

    Args:
        log_data: The execution log dict from DecisionLoop.run() or ReActAgent.run()
        filename: Optional custom filename (default: agent_log_{session_id}.json)

    Returns:
        Path to the saved log file.
    """
    session_id = log_data.get("session_id", "unknown")

    if not filename:
        filename = f"agent_log_{session_id}.json"

    # Inject agent identity if available
    if ERC8004_AGENT_ID and not log_data.get("agent_id"):
        log_data["agent_id"] = ERC8004_AGENT_ID

    filepath = LOGS_DIR / filename
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(log_data, f, indent=2, default=str)

    logger.info("Execution log saved to %s", filepath)
    return filepath


def save_canonical_log(log_data: Dict[str, Any]) -> Path:
    """Save as the canonical agent_log.json at project root."""
    from core.config import PROJECT_ROOT

    if ERC8004_AGENT_ID and not log_data.get("agent_id"):
        log_data["agent_id"] = ERC8004_AGENT_ID

    filepath = PROJECT_ROOT / "agent_log.json"
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(log_data, f, indent=2, default=str)

    logger.info("Canonical agent_log.json saved to %s", filepath)
    return filepath
