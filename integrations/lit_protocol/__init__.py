"""Lit Protocol integration — token-gated encryption for premium reports."""

from integrations.lit_protocol.access_control import (
    classify_report_tier,
    create_erc20_condition,
    create_erc721_condition,
    create_erc8004_agent_condition,
    get_free_tier_conditions,
    get_premium_tier_conditions,
)
from integrations.lit_protocol.encryption import (
    decrypt_report,
    encrypt_report,
    encrypt_string,
)

__all__ = [
    # Encryption
    "encrypt_report",
    "decrypt_report",
    "encrypt_string",
    # Access control
    "create_erc20_condition",
    "create_erc721_condition",
    "create_erc8004_agent_condition",
    "get_free_tier_conditions",
    "get_premium_tier_conditions",
    "classify_report_tier",
]
