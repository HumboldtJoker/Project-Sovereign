"""
Lit Protocol encryption for premium analysis reports.

Encrypts and decrypts market analysis reports using the Lit Protocol SDK,
enabling token-gated access to premium content.  When the Lit Python SDK
is not installed, the module falls back to a base64-encoded "demo mode"
so that the rest of the pipeline can run without hard dependencies.

SDK dependency (optional): pip install lit-python-sdk
"""

import base64
import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.config import ERC8004_IDENTITY_REGISTRY

# ---------------------------------------------------------------------------
# Optional Lit SDK import
# ---------------------------------------------------------------------------
try:
    from lit_python_sdk import LitNodeClient  # type: ignore[import-untyped]

    HAS_LIT_SDK = True
except ImportError:
    HAS_LIT_SDK = False

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
LIT_NETWORK = "cayenne"  # Lit Protocol test network for hackathon demo
DEMO_MODE_MARKER = "__lit_demo_mode__"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_lit_client() -> Any:
    """Instantiate and connect a LitNodeClient.

    Returns:
        A connected ``LitNodeClient`` instance.

    Raises:
        RuntimeError: If the SDK is not installed.
    """
    if not HAS_LIT_SDK:
        raise RuntimeError(
            "lit-python-sdk is not installed. "
            "Install with: pip install lit-python-sdk"
        )

    client = LitNodeClient(lit_network=LIT_NETWORK)
    client.connect()
    logger.debug("Connected to Lit network '%s'", LIT_NETWORK)
    return client


def _compute_data_hash(data: str) -> str:
    """Return the SHA-256 hex digest of *data*."""
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _demo_encrypt(plaintext: str) -> Dict[str, str]:
    """Base64-encode *plaintext* as a stand-in when the SDK is absent.

    This is **not** real encryption.  It exists so that the hackathon demo
    pipeline can exercise the full encrypt/decrypt flow without requiring
    the Lit network to be reachable.
    """
    encoded = base64.b64encode(plaintext.encode("utf-8")).decode("ascii")
    data_hash = _compute_data_hash(plaintext)
    return {
        "ciphertext": encoded,
        "data_hash": data_hash,
        "mode": DEMO_MODE_MARKER,
    }


def _demo_decrypt(ciphertext: str) -> str:
    """Reverse a demo-mode base64 "encryption"."""
    return base64.b64decode(ciphertext.encode("ascii")).decode("utf-8")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def encrypt_string(
    data: str,
    access_conditions: List[dict],
) -> Dict[str, Any]:
    """Encrypt an arbitrary string under Lit Protocol access conditions.

    If the Lit SDK is available the string is encrypted on the Lit network.
    Otherwise a base64-encoded demo payload is returned so the rest of the
    system can still operate.

    Args:
        data:              The plaintext string to encrypt.
        access_conditions: Lit-format access-control condition list.

    Returns:
        dict with keys ``ciphertext``, ``data_hash``, and ``metadata``
        (which includes the encryption mode and a timestamp).
    """
    timestamp = datetime.now(timezone.utc).isoformat()

    if not HAS_LIT_SDK:
        logger.warning(
            "Lit SDK not available -- using demo mode (base64 encoding, NOT real encryption)"
        )
        demo = _demo_encrypt(data)
        return {
            "ciphertext": demo["ciphertext"],
            "data_hash": demo["data_hash"],
            "metadata": {
                "mode": DEMO_MODE_MARKER,
                "note": "Base64-encoded demo -- install lit-python-sdk for real encryption",
                "encrypted_at": timestamp,
                "access_conditions": access_conditions,
            },
        }

    try:
        client = _get_lit_client()
        result = client.encrypt_string(
            data_to_encrypt=data,
            access_control_conditions=access_conditions,
            chain="ethereum",
        )

        ciphertext = result.get("ciphertext", "")
        data_hash = result.get("dataToEncryptHash", _compute_data_hash(data))

        logger.info("String encrypted via Lit network (hash=%s)", data_hash[:16])
        return {
            "ciphertext": ciphertext,
            "data_hash": data_hash,
            "metadata": {
                "mode": "lit_protocol",
                "network": LIT_NETWORK,
                "encrypted_at": timestamp,
                "access_conditions": access_conditions,
            },
        }

    except Exception as exc:
        logger.error("Lit encryption failed, falling back to demo mode: %s", exc)
        demo = _demo_encrypt(data)
        return {
            "ciphertext": demo["ciphertext"],
            "data_hash": demo["data_hash"],
            "metadata": {
                "mode": DEMO_MODE_MARKER,
                "note": f"Fallback after Lit error: {exc}",
                "encrypted_at": timestamp,
                "access_conditions": access_conditions,
            },
        }


def encrypt_report(
    report_data: dict,
    access_conditions: List[dict],
) -> Dict[str, Any]:
    """Encrypt a full market-analysis report.

    The report dict is serialised to JSON, then encrypted (or base64-encoded
    in demo mode).  Metadata such as the ticker symbol, report type, and
    generation timestamp are preserved in the clear so that catalogue
    indexing works without decryption.

    Args:
        report_data:       The analysis report payload (arbitrary dict).
        access_conditions: Lit-format access-control condition list.

    Returns:
        dict with keys:
            - ``ciphertext``:  The encrypted (or base64) payload.
            - ``data_hash``:   SHA-256 of the plaintext JSON.
            - ``metadata``:    Encryption mode, timestamp, public fields.
    """
    # Inject a generation timestamp if not already present
    report_data.setdefault(
        "encrypted_at", datetime.now(timezone.utc).isoformat()
    )

    plaintext = json.dumps(report_data, indent=2, default=str)
    result = encrypt_string(plaintext, access_conditions)

    # Attach lightweight public metadata for cataloguing
    result["metadata"]["report_type"] = report_data.get("report_type", "unknown")
    result["metadata"]["ticker"] = report_data.get("ticker", "unknown")
    result["metadata"]["report_generated_at"] = report_data.get("generated_at", "")

    logger.info(
        "Report encrypted — ticker=%s, type=%s, hash=%s",
        result["metadata"]["ticker"],
        result["metadata"]["report_type"],
        result["data_hash"][:16],
    )
    return result


def decrypt_report(
    ciphertext: str,
    data_hash: str,
    access_conditions: List[dict],
    session_sigs: Optional[dict] = None,
) -> Dict[str, Any]:
    """Decrypt an encrypted analysis report.

    In demo mode the base64 payload is simply decoded.  When the Lit SDK is
    available, ``session_sigs`` (or auth signatures) are used to prove that
    the caller satisfies the access conditions.

    Args:
        ciphertext:        The encrypted payload string.
        data_hash:         SHA-256 hash returned by the encrypt call.
        access_conditions: The same access-control conditions used to encrypt.
        session_sigs:      Lit session signatures proving the caller's eligibility.
                           Optional in demo mode.

    Returns:
        dict with keys:
            - ``success``:     Boolean indicating outcome.
            - ``report_data``: The decrypted report dict (on success).
            - ``error``:       Error message string (on failure).
    """
    # ---- Demo mode path ----
    if not HAS_LIT_SDK:
        logger.warning("Lit SDK not available -- decrypting in demo mode")
        try:
            plaintext = _demo_decrypt(ciphertext)
            report = json.loads(plaintext)

            # Verify integrity
            actual_hash = _compute_data_hash(plaintext)
            if actual_hash != data_hash:
                logger.warning(
                    "Data-hash mismatch in demo decrypt (expected %s, got %s)",
                    data_hash[:16],
                    actual_hash[:16],
                )

            return {
                "success": True,
                "report_data": report,
                "mode": DEMO_MODE_MARKER,
            }
        except Exception as exc:
            logger.error("Demo-mode decryption failed: %s", exc)
            return {"success": False, "error": str(exc)}

    # ---- Real Lit Protocol path ----
    try:
        client = _get_lit_client()
        result = client.decrypt_string(
            ciphertext=ciphertext,
            data_to_encrypt_hash=data_hash,
            access_control_conditions=access_conditions,
            chain="ethereum",
            session_sigs=session_sigs or {},
        )

        plaintext = result.get("decryptedString", "")
        report = json.loads(plaintext)

        logger.info("Report decrypted via Lit network (hash=%s)", data_hash[:16])
        return {
            "success": True,
            "report_data": report,
            "mode": "lit_protocol",
        }

    except json.JSONDecodeError as exc:
        logger.error("Decrypted payload is not valid JSON: %s", exc)
        return {"success": False, "error": f"Invalid JSON after decryption: {exc}"}
    except Exception as exc:
        logger.error("Lit decryption failed: %s", exc)
        return {"success": False, "error": str(exc)}
