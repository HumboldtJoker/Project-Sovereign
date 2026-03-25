"""
ERC-8004 Reputation Tracking and Feedback on Base L2.

Provides functions to submit feedback for agents, query reputation summaries,
and translate trade-verification results into on-chain reputation updates.

Spec: https://eips.ethereum.org/EIPS/eip-8004 (Reputation Registry section)
"""

import logging
from typing import List, Optional

from web3 import Web3
from web3.exceptions import ContractLogicError

from core.config import (
    BASE_RPC_URL,
    ERC8004_REPUTATION_REGISTRY,
    OPERATOR_PRIVATE_KEY,
    OPERATOR_WALLET,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ABI — Reputation Registry
# ---------------------------------------------------------------------------
REPUTATION_REGISTRY_ABI = [
    # ── giveFeedback ──────────────────────────────────────────────────────
    {
        "name": "giveFeedback",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "agentId", "type": "uint256"},
            {"name": "value", "type": "int128"},
            {"name": "valueDecimals", "type": "uint8"},
            {"name": "tag1", "type": "string"},
            {"name": "tag2", "type": "string"},
            {"name": "endpoint", "type": "string"},
            {"name": "feedbackURI", "type": "string"},
            {"name": "feedbackHash", "type": "bytes32"},
        ],
        "outputs": [],
    },
    # ── getSummary ────────────────────────────────────────────────────────
    {
        "name": "getSummary",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "agentId", "type": "uint256"},
            {"name": "clientAddresses", "type": "address[]"},
            {"name": "tag1", "type": "string"},
            {"name": "tag2", "type": "string"},
        ],
        "outputs": [
            {"name": "count", "type": "uint64"},
            {"name": "summaryValue", "type": "int128"},
            {"name": "summaryValueDecimals", "type": "uint8"},
        ],
    },
    # ── readAllFeedback ───────────────────────────────────────────────────
    {
        "name": "readAllFeedback",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "agentId", "type": "uint256"},
            {"name": "clientAddresses", "type": "address[]"},
            {"name": "tag1", "type": "string"},
            {"name": "tag2", "type": "string"},
            {"name": "includeRevoked", "type": "bool"},
        ],
        "outputs": [
            {"name": "clients", "type": "address[]"},
            {"name": "feedbackIndexes", "type": "uint64[]"},
            {"name": "values", "type": "int128[]"},
            {"name": "valueDecimals", "type": "uint8[]"},
            {"name": "tag1s", "type": "string[]"},
            {"name": "tag2s", "type": "string[]"},
            {"name": "revokedStatuses", "type": "bool[]"},
        ],
    },
    # ── Events ────────────────────────────────────────────────────────────
    {
        "name": "FeedbackGiven",
        "type": "event",
        "inputs": [
            {"name": "agentId", "type": "uint256", "indexed": True},
            {"name": "client", "type": "address", "indexed": True},
            {"name": "value", "type": "int128", "indexed": False},
            {"name": "tag1", "type": "string", "indexed": False},
            {"name": "tag2", "type": "string", "indexed": False},
        ],
    },
]

# ---------------------------------------------------------------------------
# Default gas / chain parameters
# ---------------------------------------------------------------------------
DEFAULT_GAS_LIMIT = 400_000
BASE_CHAIN_ID = 8453


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _check_credentials() -> Optional[dict]:
    """Return an error dict if wallet/key are not configured, else None."""
    if not OPERATOR_WALLET or not OPERATOR_PRIVATE_KEY:
        msg = (
            "OPERATOR_WALLET and OPERATOR_PRIVATE_KEY must be set in .env "
            "to sign on-chain transactions."
        )
        logger.warning(msg)
        return {"success": False, "error": msg}
    return None


def _get_web3() -> Web3:
    """Return a connected Web3 instance for Base L2."""
    w3 = Web3(Web3.HTTPProvider(BASE_RPC_URL))
    if not w3.is_connected():
        raise ConnectionError(f"Cannot connect to Base RPC at {BASE_RPC_URL}")
    return w3


def _get_contract(w3: Web3):
    """Return the Reputation Registry contract object."""
    return w3.eth.contract(
        address=Web3.to_checksum_address(ERC8004_REPUTATION_REGISTRY),
        abi=REPUTATION_REGISTRY_ABI,
    )


def _build_and_send(w3: Web3, tx_builder, description: str) -> dict:
    """
    Build a transaction from *tx_builder*, sign, send, wait for receipt.
    """
    account = Web3.to_checksum_address(OPERATOR_WALLET)
    nonce = w3.eth.get_transaction_count(account)

    tx = tx_builder.build_transaction({
        "from": account,
        "nonce": nonce,
        "gas": DEFAULT_GAS_LIMIT,
        "chainId": BASE_CHAIN_ID,
    })

    signed = w3.eth.account.sign_transaction(tx, OPERATOR_PRIVATE_KEY)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    logger.info("%s — tx sent: %s", description, tx_hash.hex())

    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

    if receipt.status != 1:
        msg = f"{description} — transaction reverted (tx={tx_hash.hex()})"
        logger.error(msg)
        return {"success": False, "error": msg, "tx_hash": tx_hash.hex()}

    logger.info(
        "%s — confirmed in block %s (gas used: %s)",
        description, receipt.blockNumber, receipt.gasUsed,
    )
    return {
        "success": True,
        "tx_hash": tx_hash.hex(),
        "block_number": receipt.blockNumber,
        "gas_used": receipt.gasUsed,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def give_feedback(
    agent_id: int,
    value: int,
    value_decimals: int,
    tag1: str = "",
    tag2: str = "",
    endpoint: str = "",
    feedback_uri: str = "",
    feedback_hash: bytes = b"",
) -> dict:
    """
    Submit reputation feedback for an agent.

    Args:
        agent_id:       The on-chain agentId to rate.
        value:          Signed integer score (int128). Positive = good, negative = bad.
        value_decimals: Number of decimal places in *value* (uint8).
        tag1:           Primary classification tag (e.g. ``"market_analysis"``).
        tag2:           Secondary classification tag (e.g. ``"accuracy"``).
        endpoint:       Service endpoint the feedback refers to (optional).
        feedback_uri:   URI pointing to detailed feedback JSON (optional).
        feedback_hash:  bytes32 hash of the feedback document (optional).
                        Will be zero-padded to 32 bytes if shorter.

    Returns:
        dict with ``success`` and transaction details, or an error dict.
    """
    cred_err = _check_credentials()
    if cred_err:
        return cred_err

    try:
        w3 = _get_web3()
        contract = _get_contract(w3)

        # Ensure feedback_hash is exactly 32 bytes.
        if len(feedback_hash) < 32:
            feedback_hash = feedback_hash.ljust(32, b"\x00")
        elif len(feedback_hash) > 32:
            feedback_hash = feedback_hash[:32]

        tx_builder = contract.functions.giveFeedback(
            agent_id,
            value,
            value_decimals,
            tag1,
            tag2,
            endpoint,
            feedback_uri,
            feedback_hash,
        )

        result = _build_and_send(
            w3, tx_builder,
            f"giveFeedback(agentId={agent_id}, value={value})",
        )
        return result

    except ContractLogicError as exc:
        logger.error("Contract reverted during give_feedback: %s", exc)
        return {"success": False, "error": f"Contract revert: {exc}"}
    except Exception as exc:
        logger.exception("give_feedback failed")
        return {"success": False, "error": str(exc)}


def get_reputation_summary(
    agent_id: int,
    client_addresses: Optional[List[str]] = None,
    tag1: str = "",
    tag2: str = "",
) -> dict:
    """
    Read the aggregated reputation summary for an agent (view call — no gas).

    Args:
        agent_id:         On-chain agentId.
        client_addresses: Filter to feedback from these addresses only.
                          Pass ``None`` or ``[]`` for unfiltered.
        tag1:             Filter by primary tag (empty string = all).
        tag2:             Filter by secondary tag (empty string = all).

    Returns:
        dict with ``success``, ``count``, ``summary_value``, and
        ``summary_value_decimals`` — or an error dict.
    """
    try:
        w3 = _get_web3()
        contract = _get_contract(w3)

        addresses = [
            Web3.to_checksum_address(a) for a in (client_addresses or [])
        ]

        count, summary_value, summary_value_decimals = (
            contract.functions.getSummary(agent_id, addresses, tag1, tag2).call()
        )

        logger.debug(
            "getSummary(agentId=%s) → count=%s value=%s decimals=%s",
            agent_id, count, summary_value, summary_value_decimals,
        )

        return {
            "success": True,
            "agent_id": agent_id,
            "count": count,
            "summary_value": summary_value,
            "summary_value_decimals": summary_value_decimals,
        }

    except Exception as exc:
        logger.exception("get_reputation_summary failed for agentId=%s", agent_id)
        return {"success": False, "error": str(exc)}


def read_all_feedback(agent_id: int) -> list:
    """
    Read every feedback entry for an agent (view call — no gas).

    Returns a list of dicts, each containing:
        ``client``, ``feedback_index``, ``value``, ``value_decimals``,
        ``tag1``, ``tag2``, ``revoked``.

    Returns an empty list on error (error is logged).
    """
    try:
        w3 = _get_web3()
        contract = _get_contract(w3)

        (
            clients,
            feedback_indexes,
            values,
            value_decimals,
            tag1s,
            tag2s,
            revoked_statuses,
        ) = contract.functions.readAllFeedback(
            agent_id,
            [],     # clientAddresses — unfiltered
            "",     # tag1 — all
            "",     # tag2 — all
            True,   # includeRevoked
        ).call()

        entries = []
        for i in range(len(clients)):
            entries.append({
                "client": clients[i],
                "feedback_index": feedback_indexes[i],
                "value": values[i],
                "value_decimals": value_decimals[i],
                "tag1": tag1s[i],
                "tag2": tag2s[i],
                "revoked": revoked_statuses[i],
            })

        logger.debug("readAllFeedback(agentId=%s) → %d entries", agent_id, len(entries))
        return entries

    except Exception as exc:
        logger.exception("read_all_feedback failed for agentId=%s", agent_id)
        return []


# ---------------------------------------------------------------------------
# Convenience: translate trade results into reputation feedback
# ---------------------------------------------------------------------------

# Scoring constants (2-decimal fixed-point: 100 = 1.00)
_SCORE_PROFITABLE = 100          # +1.00
_SCORE_BREAK_EVEN = 50           # +0.50
_SCORE_SMALL_LOSS = -25          # -0.25
_SCORE_LARGE_LOSS = -75          # -0.75
_VALUE_DECIMALS = 2

# Threshold for "large" loss: 5% or more of position value
_LARGE_LOSS_THRESHOLD_PCT = 5.0


def update_reputation_after_trade(agent_id: int, trade_result: dict) -> dict:
    """
    Convenience wrapper that converts a trade verification result into a
    reputation feedback entry and submits it on-chain.

    Expected keys in *trade_result*:

    - ``ticker`` (str):  The traded symbol.
    - ``action`` (str):  BUY / SELL / SHORT / COVER.
    - ``pnl`` (float):   Realized profit/loss in dollars (positive = profit).
    - ``pnl_pct`` (float, optional): P&L as a percentage of position value.
    - ``execution_log_cid`` (str, optional): Storacha CID of the execution log.

    Returns:
        dict from :func:`give_feedback`, augmented with the computed
        ``score`` and ``tag`` that were submitted.
    """
    cred_err = _check_credentials()
    if cred_err:
        return cred_err

    pnl = trade_result.get("pnl", 0.0)
    pnl_pct = trade_result.get("pnl_pct", 0.0)
    ticker = trade_result.get("ticker", "unknown")
    action = trade_result.get("action", "unknown")
    cid = trade_result.get("execution_log_cid", "")

    # Determine score.
    if pnl > 0:
        score = _SCORE_PROFITABLE
    elif pnl == 0:
        score = _SCORE_BREAK_EVEN
    elif abs(pnl_pct) < _LARGE_LOSS_THRESHOLD_PCT:
        score = _SCORE_SMALL_LOSS
    else:
        score = _SCORE_LARGE_LOSS

    tag1 = "trade_execution"
    tag2 = f"{action.lower()}_{ticker.upper()}"
    feedback_uri = f"ipfs://{cid}" if cid else ""

    logger.info(
        "Submitting reputation feedback for agentId=%s: score=%s tag1=%s tag2=%s",
        agent_id, score, tag1, tag2,
    )

    result = give_feedback(
        agent_id=agent_id,
        value=score,
        value_decimals=_VALUE_DECIMALS,
        tag1=tag1,
        tag2=tag2,
        endpoint="",
        feedback_uri=feedback_uri,
        feedback_hash=b"",
    )

    result["score"] = score
    result["tag1"] = tag1
    result["tag2"] = tag2
    return result
