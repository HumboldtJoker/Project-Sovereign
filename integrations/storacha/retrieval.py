"""
Storacha retrieval integration for Sovereign Market Intelligence Agent.

Retrieves content from Storacha (IPFS/Filecoin) by CID via the public
HTTP gateway and optionally verifies content integrity.
"""

import hashlib
import logging
from typing import Any, Dict, Optional

import requests

from core.config import STORACHA_SPACE

logger = logging.getLogger(__name__)

STORACHA_GATEWAY = "https://storacha.link/ipfs"
DEFAULT_TIMEOUT = 30  # seconds


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_gateway_url(cid: str, filename: str = "") -> str:
    """Build a public gateway URL for a given CID and optional filename.

    Args:
        cid:      Content identifier (CIDv1).
        filename: Optional filename appended to the path.  When the content
                  was uploaded as a single file wrapped in a directory (the
                  default ``storacha up`` behaviour), the filename is needed
                  to address the file inside the directory CID.

    Returns:
        Full HTTPS gateway URL.
    """
    if not cid:
        raise ValueError("CID must not be empty")

    base = f"{STORACHA_GATEWAY}/{cid}"
    if filename:
        base = f"{base}/{filename}"
    return base


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def retrieve_by_cid(
    cid: str,
    filename: str = "",
    timeout: int = DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    """Fetch content from the Storacha gateway by CID.

    Args:
        cid:      Content identifier.
        filename: Optional filename within the CID directory.
        timeout:  HTTP request timeout in seconds.

    Returns:
        dict with keys:
            - cid:          The requested CID
            - gateway_url:  The URL that was fetched
            - status_code:  HTTP status code
            - content_type: Content-Type header from the response
            - size_bytes:   Length of the response body
            - content:      Response body -- decoded text for JSON/text types,
                            raw bytes otherwise
            - sha256:       Hex-encoded SHA-256 of the response body
    """
    url = get_gateway_url(cid, filename)
    logger.debug("Fetching %s", url)

    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
    except requests.ConnectionError as exc:
        logger.error("Connection error retrieving CID %s: %s", cid, exc)
        return {
            "cid": cid,
            "gateway_url": url,
            "status_code": None,
            "error": f"Connection error: {exc}",
        }
    except requests.Timeout:
        logger.error("Timeout retrieving CID %s after %ds", cid, timeout)
        return {
            "cid": cid,
            "gateway_url": url,
            "status_code": None,
            "error": f"Request timed out after {timeout}s",
        }
    except requests.HTTPError as exc:
        logger.error("HTTP error retrieving CID %s: %s", cid, exc)
        return {
            "cid": cid,
            "gateway_url": url,
            "status_code": resp.status_code,
            "error": str(exc),
        }

    raw = resp.content
    content_type = resp.headers.get("Content-Type", "")

    # Decode text-like content for convenience
    if "json" in content_type or "text" in content_type:
        body: Any = resp.text
    else:
        body = raw

    result = {
        "cid": cid,
        "gateway_url": url,
        "status_code": resp.status_code,
        "content_type": content_type,
        "size_bytes": len(raw),
        "content": body,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    logger.info("Retrieved CID %s (%d bytes)", cid, len(raw))
    return result


def verify_content(
    cid: str,
    expected_hash: str = "",
    filename: str = "",
    timeout: int = DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    """Retrieve content by CID and verify its integrity.

    If *expected_hash* is provided it is compared against the SHA-256 of the
    downloaded bytes.  The verification result is included in the return dict
    regardless, so callers can inspect ``sha256`` even when no expected hash
    is supplied.

    Args:
        cid:           Content identifier.
        expected_hash: Optional hex-encoded SHA-256 to verify against.
        filename:      Optional filename within the CID directory.
        timeout:       HTTP request timeout in seconds.

    Returns:
        dict with the same keys as :func:`retrieve_by_cid` plus:
            - verified:       ``True`` if hash matches, ``False`` if mismatch,
                              ``None`` if no expected_hash was given.
            - expected_hash:  The hash that was checked (empty string if none).
    """
    result = retrieve_by_cid(cid, filename=filename, timeout=timeout)

    if "error" in result:
        result["verified"] = None
        result["expected_hash"] = expected_hash
        return result

    actual_hash = result.get("sha256", "")

    if expected_hash:
        match = actual_hash == expected_hash.lower()
        result["verified"] = match
        if match:
            logger.info("Content verification passed for CID %s", cid)
        else:
            logger.warning(
                "Content verification FAILED for CID %s: "
                "expected %s, got %s",
                cid,
                expected_hash,
                actual_hash,
            )
    else:
        result["verified"] = None
        logger.debug(
            "No expected hash provided; skipping verification for CID %s", cid
        )

    result["expected_hash"] = expected_hash
    return result
