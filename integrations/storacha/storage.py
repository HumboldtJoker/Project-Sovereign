"""
Storacha storage integration for Sovereign.

Uploads execution logs and analysis reports to Storacha (IPFS/Filecoin)
via the Storacha CLI.  This provides decentralized, content-addressed
storage for the Agents With Receipts challenge track.

CLI dependency: npm install -g @storacha/cli
"""

import hashlib
import json
import logging
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from core.config import LOGS_DIR, STORACHA_SPACE

logger = logging.getLogger(__name__)

STORACHA_GATEWAY = "https://storacha.link/ipfs"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_cli_available() -> bool:
    """Return True if the ``storacha`` CLI is installed and on PATH."""
    return shutil.which("storacha") is not None


def _parse_cli_output(output: str, filename: str) -> Dict[str, str]:
    """Extract the CID and gateway URL from ``storacha up`` output.

    The CLI prints a gateway URL of the form:
        https://storacha.link/ipfs/<CID>/<filename>

    We parse out the CID and reconstruct a clean URL.
    """
    # Look for a URL in the output
    url_match = re.search(
        r"https?://\S*storacha\.link/ipfs/([A-Za-z0-9]+)(?:/\S*)?", output
    )
    if url_match:
        cid = url_match.group(1)
        gateway_url = f"{STORACHA_GATEWAY}/{cid}/{filename}"
        return {"cid": cid, "gateway_url": gateway_url}

    # Fallback: look for a bare CIDv1 (bafy...) anywhere in the output
    cid_match = re.search(r"\b(bafy[A-Za-z0-9]{50,})\b", output)
    if cid_match:
        cid = cid_match.group(1)
        gateway_url = f"{STORACHA_GATEWAY}/{cid}/{filename}"
        return {"cid": cid, "gateway_url": gateway_url}

    raise ValueError(f"Could not parse CID from storacha CLI output: {output!r}")


def _run_storacha_upload(file_path: str) -> str:
    """Run ``storacha up <file_path>`` and return the raw stdout."""
    cmd = ["storacha", "up", file_path]
    logger.debug("Running: %s", " ".join(cmd))

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=120,
    )

    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise RuntimeError(
            f"storacha upload failed (exit {result.returncode}): {stderr}"
        )

    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def upload_file(file_path: str) -> Dict[str, Any]:
    """Upload an arbitrary file to Storacha and return metadata.

    Args:
        file_path: Absolute or relative path to the file to upload.

    Returns:
        dict with keys:
            - cid:         Content identifier (CIDv1)
            - gateway_url: Public gateway URL to retrieve the file
            - filename:    Basename of the uploaded file
            - size_bytes:  Size of the uploaded file
            - sha256:      Hex-encoded SHA-256 of the file contents
            - uploaded_at: ISO-8601 timestamp
    """
    path = Path(file_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"File not found: {path}")

    if not is_cli_available():
        raise EnvironmentError(
            "storacha CLI is not installed. "
            "Install with: npm install -g @storacha/cli"
        )

    # Compute content hash before upload
    sha256 = hashlib.sha256(path.read_bytes()).hexdigest()

    raw_output = _run_storacha_upload(str(path))
    parsed = _parse_cli_output(raw_output, path.name)

    result = {
        "cid": parsed["cid"],
        "gateway_url": parsed["gateway_url"],
        "filename": path.name,
        "size_bytes": path.stat().st_size,
        "sha256": sha256,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
    }
    logger.info("Uploaded %s -> CID %s", path.name, result["cid"])
    return result


def upload_execution_log(log_data: Dict[str, Any]) -> Dict[str, Any]:
    """Serialize an execution log dict to a temp JSON file and upload it.

    Args:
        log_data: Structured execution log (from DecisionLoop / ReActAgent).

    Returns:
        Same dict as :func:`upload_file` with an additional ``session_id`` key.
    """
    session_id = log_data.get("session_id", "unknown")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"agent_log_{session_id}_{timestamp}.json"

    # Also persist locally for safety
    local_path = LOGS_DIR / filename
    local_path.parent.mkdir(parents=True, exist_ok=True)
    with open(local_path, "w", encoding="utf-8") as f:
        json.dump(log_data, f, indent=2, default=str)
    logger.debug("Execution log written locally: %s", local_path)

    result = upload_file(str(local_path))
    result["session_id"] = session_id
    logger.info(
        "Execution log for session %s uploaded -> CID %s",
        session_id,
        result["cid"],
    )
    return result


def upload_analysis_report(
    report_data: Dict[str, Any],
    ticker: str,
) -> Dict[str, Any]:
    """Upload a market-analysis report for a given ticker.

    The report is serialized as JSON and uploaded to Storacha.

    Args:
        report_data: Dict containing the analysis payload.
        ticker:      Stock ticker symbol (e.g. ``"AAPL"``).

    Returns:
        Same dict as :func:`upload_file` with an additional ``ticker`` key.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"analysis_{ticker}_{timestamp}.json"

    # Inject metadata
    report_data.setdefault("ticker", ticker)
    report_data.setdefault("generated_at", datetime.now(timezone.utc).isoformat())

    tmp_dir = Path(tempfile.mkdtemp(prefix="storacha_"))
    tmp_path = tmp_dir / filename
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(report_data, f, indent=2, default=str)

    try:
        result = upload_file(str(tmp_path))
    finally:
        # Clean up temp file and directory
        tmp_path.unlink(missing_ok=True)
        try:
            tmp_dir.rmdir()
        except OSError:
            pass

    result["ticker"] = ticker
    logger.info(
        "Analysis report for %s uploaded -> CID %s", ticker, result["cid"]
    )
    return result
