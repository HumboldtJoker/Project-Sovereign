"""ERC-8004 integration — agent identity and reputation on Base L2."""

from integrations.erc8004.identity import (
    get_agent_wallet,
    get_metadata,
    register_agent,
    set_agent_uri,
    set_metadata,
)
from integrations.erc8004.reputation import (
    get_reputation_summary,
    give_feedback,
    read_all_feedback,
    update_reputation_after_trade,
)

__all__ = [
    # Identity
    "register_agent",
    "set_agent_uri",
    "set_metadata",
    "get_metadata",
    "get_agent_wallet",
    # Reputation
    "give_feedback",
    "get_reputation_summary",
    "read_all_feedback",
    "update_reputation_after_trade",
]
