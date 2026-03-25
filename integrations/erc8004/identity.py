"""
ERC-8004 Agent Identity Registration on Base L2.

Interacts with the Identity Registry (ERC-721 based) to register agents,
update URIs, and manage metadata key-value pairs on-chain.

Contract: 0x8004A169FB4a3325136EB29fA0ceB6D2e539a432 on Base L2
Spec: https://eips.ethereum.org/EIPS/eip-8004
"""

import logging
from typing import Optional

from web3 import Web3
from web3.exceptions import ContractLogicError

from core.config import (
    BASE_RPC_URL,
    ERC8004_IDENTITY_REGISTRY,
    OPERATOR_PRIVATE_KEY,
    OPERATOR_WALLET,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ABI — ERC-721 + URIStorage Identity Registry
# Only the functions we actually call are included to keep the module lean.
# ---------------------------------------------------------------------------
IDENTITY_REGISTRY_ABI = [
    # ── Registration ──────────────────────────────────────────────────────
    {
        "name": "register",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "agentURI", "type": "string"},
        ],
        "outputs": [
            {"name": "agentId", "type": "uint256"},
        ],
    },
    {
        "name": "register",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [],
        "outputs": [
            {"name": "agentId", "type": "uint256"},
        ],
    },
    # ── URI management ────────────────────────────────────────────────────
    {
        "name": "setAgentURI",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "agentId", "type": "uint256"},
            {"name": "newURI", "type": "string"},
        ],
        "outputs": [],
    },
    # ── Metadata key-value store ──────────────────────────────────────────
    {
        "name": "setMetadata",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "agentId", "type": "uint256"},
            {"name": "metadataKey", "type": "string"},
            {"name": "metadataValue", "type": "bytes"},
        ],
        "outputs": [],
    },
    {
        "name": "getMetadata",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "agentId", "type": "uint256"},
            {"name": "metadataKey", "type": "string"},
        ],
        "outputs": [
            {"name": "", "type": "bytes"},
        ],
    },
    # ── Agent wallet ──────────────────────────────────────────────────────
    {
        "name": "getAgentWallet",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "agentId", "type": "uint256"},
        ],
        "outputs": [
            {"name": "", "type": "address"},
        ],
    },
    {
        "name": "setAgentWallet",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "agentId", "type": "uint256"},
            {"name": "newWallet", "type": "address"},
            {"name": "deadline", "type": "uint256"},
            {"name": "signature", "type": "bytes"},
        ],
        "outputs": [],
    },
    # ── ERC-721 ownerOf (useful for verifying ownership) ──────────────────
    {
        "name": "ownerOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "tokenId", "type": "uint256"},
        ],
        "outputs": [
            {"name": "", "type": "address"},
        ],
    },
    # ── Events ────────────────────────────────────────────────────────────
    {
        "name": "Registered",
        "type": "event",
        "inputs": [
            {"name": "agentId", "type": "uint256", "indexed": True},
            {"name": "agentURI", "type": "string", "indexed": False},
            {"name": "owner", "type": "address", "indexed": True},
        ],
    },
    {
        "name": "URIUpdated",
        "type": "event",
        "inputs": [
            {"name": "agentId", "type": "uint256", "indexed": True},
            {"name": "newURI", "type": "string", "indexed": False},
            {"name": "updatedBy", "type": "address", "indexed": True},
        ],
    },
    {
        "name": "MetadataSet",
        "type": "event",
        "inputs": [
            {"name": "agentId", "type": "uint256", "indexed": True},
            {"name": "indexedMetadataKey", "type": "string", "indexed": True},
            {"name": "metadataKey", "type": "string", "indexed": False},
            {"name": "metadataValue", "type": "bytes", "indexed": False},
        ],
    },
    # ── Standard ERC-721 Transfer event (emitted on register) ─────────────
    {
        "name": "Transfer",
        "type": "event",
        "inputs": [
            {"name": "from", "type": "address", "indexed": True},
            {"name": "to", "type": "address", "indexed": True},
            {"name": "tokenId", "type": "uint256", "indexed": True},
        ],
    },
]

# ---------------------------------------------------------------------------
# Default gas parameters for Base L2
# ---------------------------------------------------------------------------
DEFAULT_GAS_LIMIT = 350_000
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
    """Return the Identity Registry contract object."""
    return w3.eth.contract(
        address=Web3.to_checksum_address(ERC8004_IDENTITY_REGISTRY),
        abi=IDENTITY_REGISTRY_ABI,
    )


def _build_and_send(w3: Web3, tx_builder, description: str) -> dict:
    """
    Build a transaction from *tx_builder* (a contract function call),
    sign it, send it, wait for the receipt, and return a result dict.
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

def register_agent(agent_uri: str) -> dict:
    """
    Register a new agent on the Identity Registry.

    Args:
        agent_uri: URI that resolves to the agent registration JSON
                   (typically an IPFS/Storacha CID URL).

    Returns:
        dict with ``success``, ``agent_id``, ``tx_hash``, and block info
        — or an error dict on failure.
    """
    cred_err = _check_credentials()
    if cred_err:
        return cred_err

    try:
        w3 = _get_web3()
        contract = _get_contract(w3)

        # Use the register(string) overload that accepts the URI directly.
        tx_builder = contract.functions.register(agent_uri)
        result = _build_and_send(w3, tx_builder, "register_agent")

        if not result["success"]:
            return result

        # Extract agentId from the Registered event in the receipt logs.
        tx_hash_bytes = bytes.fromhex(result["tx_hash"])
        receipt = w3.eth.get_transaction_receipt(tx_hash_bytes)
        registered_events = contract.events.Registered().process_receipt(receipt)

        if registered_events:
            agent_id = registered_events[0]["args"]["agentId"]
        else:
            # Fallback: try the Transfer event (ERC-721 mint).
            transfer_events = contract.events.Transfer().process_receipt(receipt)
            agent_id = transfer_events[0]["args"]["tokenId"] if transfer_events else None

        result["agent_id"] = agent_id
        logger.info("Agent registered — agentId=%s, uri=%s", agent_id, agent_uri)
        return result

    except ContractLogicError as exc:
        logger.error("Contract reverted during register_agent: %s", exc)
        return {"success": False, "error": f"Contract revert: {exc}"}
    except Exception as exc:
        logger.exception("register_agent failed")
        return {"success": False, "error": str(exc)}


def set_agent_uri(agent_id: int, new_uri: str) -> dict:
    """
    Update the URI associated with an existing agent.

    Args:
        agent_id: On-chain agentId (ERC-721 tokenId).
        new_uri:  New URI that resolves to the agent registration JSON.

    Returns:
        dict with ``success`` and transaction details.
    """
    cred_err = _check_credentials()
    if cred_err:
        return cred_err

    try:
        w3 = _get_web3()
        contract = _get_contract(w3)
        tx_builder = contract.functions.setAgentURI(agent_id, new_uri)
        result = _build_and_send(w3, tx_builder, f"setAgentURI(agentId={agent_id})")
        return result

    except ContractLogicError as exc:
        logger.error("Contract reverted during set_agent_uri: %s", exc)
        return {"success": False, "error": f"Contract revert: {exc}"}
    except Exception as exc:
        logger.exception("set_agent_uri failed")
        return {"success": False, "error": str(exc)}


def set_metadata(agent_id: int, key: str, value: bytes) -> dict:
    """
    Set a metadata key-value pair for an agent.

    Args:
        agent_id: On-chain agentId.
        key:      Metadata key string (e.g. ``"execution_log_cid"``).
        value:    Arbitrary bytes value.

    Returns:
        dict with ``success`` and transaction details.
    """
    cred_err = _check_credentials()
    if cred_err:
        return cred_err

    try:
        w3 = _get_web3()
        contract = _get_contract(w3)
        tx_builder = contract.functions.setMetadata(agent_id, key, value)
        result = _build_and_send(w3, tx_builder, f"setMetadata(agentId={agent_id}, key={key!r})")
        return result

    except ContractLogicError as exc:
        logger.error("Contract reverted during set_metadata: %s", exc)
        return {"success": False, "error": f"Contract revert: {exc}"}
    except Exception as exc:
        logger.exception("set_metadata failed")
        return {"success": False, "error": str(exc)}


def get_metadata(agent_id: int, key: str) -> bytes:
    """
    Read a metadata value for an agent (view call — no gas).

    Args:
        agent_id: On-chain agentId.
        key:      Metadata key string.

    Returns:
        The raw bytes value, or ``b""`` on error.
    """
    try:
        w3 = _get_web3()
        contract = _get_contract(w3)
        data: bytes = contract.functions.getMetadata(agent_id, key).call()
        logger.debug("getMetadata(agentId=%s, key=%r) → %d bytes", agent_id, key, len(data))
        return data

    except Exception as exc:
        logger.exception("get_metadata failed for agentId=%s key=%r", agent_id, key)
        return b""


def get_agent_wallet(agent_id: int) -> str:
    """
    Retrieve the agent wallet address associated with an agentId.

    Args:
        agent_id: On-chain agentId.

    Returns:
        Checksummed wallet address, or ``""`` on error.
    """
    try:
        w3 = _get_web3()
        contract = _get_contract(w3)
        wallet: str = contract.functions.getAgentWallet(agent_id).call()
        logger.debug("getAgentWallet(agentId=%s) → %s", agent_id, wallet)
        return wallet

    except Exception as exc:
        logger.exception("get_agent_wallet failed for agentId=%s", agent_id)
        return ""
