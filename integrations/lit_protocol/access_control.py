"""
Lit Protocol access-control conditions for token-gated report tiers.

Builds condition objects in the Lit Protocol unified access-control format
so that premium analysis reports can be gated behind ERC-20 token holdings,
NFT ownership, or ERC-8004 agent registration.

Lit condition spec:
  https://developer.litprotocol.com/sdk/access-control/evm/basic-examples
"""

import logging
from typing import Any, Dict, List

from core.config import ERC8004_IDENTITY_REGISTRY

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Premium tier keywords used by classify_report_tier
# ---------------------------------------------------------------------------
_PREMIUM_REPORT_TYPES = {
    "congressional_pattern",
    "congressional_aggregate",
    "macro_overlay",
    "composite_signal",
    "full_analysis",
}

_PREMIUM_DATA_KEYS = {
    "congressional_trades",
    "congressional_patterns",
    "macro_regime",
    "macro_overlay",
    "regime_modifier",
    "composite_score",
}


# ---------------------------------------------------------------------------
# Condition builders
# ---------------------------------------------------------------------------

def create_erc20_condition(
    token_address: str,
    min_balance: str = "1000000000000000000",
    chain: str = "ethereum",
) -> List[Dict[str, Any]]:
    """Build a Lit access-control condition requiring an ERC-20 balance.

    The condition uses the ``balanceOf`` method to check that the caller
    holds at least *min_balance* (in the token's smallest unit, e.g. wei).

    Args:
        token_address: The ERC-20 contract address.
        min_balance:   Minimum balance as a decimal string (default: 1e18).
        chain:         Target chain name (default: ``"ethereum"``).

    Returns:
        A single-element list containing the Lit condition object.
    """
    condition = {
        "contractAddress": token_address,
        "standardContractType": "ERC20",
        "chain": chain,
        "method": "balanceOf",
        "parameters": [":userAddress"],
        "returnValueTest": {
            "comparator": ">=",
            "value": min_balance,
        },
    }
    logger.debug(
        "ERC-20 condition: token=%s, min=%s, chain=%s",
        token_address,
        min_balance,
        chain,
    )
    return [condition]


def create_erc721_condition(
    nft_address: str,
    chain: str = "ethereum",
) -> List[Dict[str, Any]]:
    """Build a Lit access-control condition requiring NFT ownership.

    Uses the ERC-721 ``balanceOf`` method to verify the caller owns at
    least one token from the collection.

    Args:
        nft_address: The ERC-721 contract address.
        chain:       Target chain name (default: ``"ethereum"``).

    Returns:
        A single-element list containing the Lit condition object.
    """
    condition = {
        "contractAddress": nft_address,
        "standardContractType": "ERC721",
        "chain": chain,
        "method": "balanceOf",
        "parameters": [":userAddress"],
        "returnValueTest": {
            "comparator": ">",
            "value": "0",
        },
    }
    logger.debug("ERC-721 condition: nft=%s, chain=%s", nft_address, chain)
    return [condition]


def create_erc8004_agent_condition(
    agent_id: int,
    registry_address: str = "",
) -> List[Dict[str, Any]]:
    """Build a Lit condition based on ERC-8004 agent ownership.

    Checks the Identity Registry (which is ERC-721 based) to verify that
    the caller is the ``ownerOf`` the specified agent token.

    Args:
        agent_id:         On-chain agentId (ERC-721 tokenId).
        registry_address: Identity Registry contract address.  Falls back
                          to the address in ``core.config``.

    Returns:
        A single-element list containing the Lit condition object.
    """
    address = registry_address or ERC8004_IDENTITY_REGISTRY
    condition = {
        "contractAddress": address,
        "standardContractType": "ERC721",
        "chain": "base",
        "method": "ownerOf",
        "parameters": [str(agent_id)],
        "returnValueTest": {
            "comparator": "=",
            "value": ":userAddress",
        },
    }
    logger.debug(
        "ERC-8004 agent condition: agentId=%s, registry=%s", agent_id, address
    )
    return [condition]


# ---------------------------------------------------------------------------
# Tier helpers
# ---------------------------------------------------------------------------

def get_free_tier_conditions() -> List[Dict[str, Any]]:
    """Return open access conditions (no token gating).

    Free-tier reports are world-readable, so we return an empty list
    signalling that no access check is required.

    Returns:
        An empty list.
    """
    logger.debug("Free-tier conditions: open access (no gating)")
    return []


def get_premium_tier_conditions(token_address: str) -> List[Dict[str, Any]]:
    """Return the standard premium-tier condition set.

    Premium access requires holding at least 1 full token (1e18 smallest
    units) of the specified ERC-20.

    Args:
        token_address: The ERC-20 contract address for the governance /
                       access token.

    Returns:
        A Lit condition list suitable for ``encrypt_report``.
    """
    conditions = create_erc20_condition(
        token_address=token_address,
        min_balance="1000000000000000000",  # 1 token (18 decimals)
        chain="ethereum",
    )
    logger.debug(
        "Premium-tier conditions: hold >= 1 token at %s", token_address
    )
    return conditions


def classify_report_tier(report_data: dict) -> str:
    """Determine whether a report warrants free or premium gating.

    Classification rules:
        * **Premium** -- the report contains congressional trading patterns,
          macro-regime overlays, composite signals, or is explicitly typed
          as one of the premium report types.
        * **Free** -- basic technical analysis, sentiment summaries, and
          any report that does not match the premium criteria.

    Args:
        report_data: The analysis report dict.  The function inspects
                     ``report_type``, top-level keys, and nested
                     ``analysis_components``.

    Returns:
        ``"premium"`` or ``"free"``.
    """
    # 1. Check explicit report_type field
    report_type = report_data.get("report_type", "").lower().strip()
    if report_type in _PREMIUM_REPORT_TYPES:
        logger.info("Report classified as premium (report_type=%r)", report_type)
        return "premium"

    # 2. Check for premium data keys at the top level
    top_keys = set(report_data.keys())
    premium_matches = top_keys & _PREMIUM_DATA_KEYS
    if premium_matches:
        logger.info(
            "Report classified as premium (keys: %s)",
            ", ".join(sorted(premium_matches)),
        )
        return "premium"

    # 3. Check nested analysis_components list / dict
    components = report_data.get("analysis_components", [])
    if isinstance(components, dict):
        components = list(components.keys())
    if isinstance(components, list):
        for comp in components:
            comp_lower = str(comp).lower()
            if any(kw in comp_lower for kw in ("congress", "macro", "composite")):
                logger.info(
                    "Report classified as premium (component=%r)", comp
                )
                return "premium"

    # 4. Default to free tier
    logger.debug("Report classified as free tier")
    return "free"
