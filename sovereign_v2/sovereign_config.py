"""Sovereign shared configuration — env-only secrets, shared constants.

SECURITY: No API keys in source. Keys come from the environment only:
  - Alpaca/FRED: .envrc in this directory  (direnv or `source`)
  - Anthropic:   ~/.coalition/secrets.env
Cron sources both (see sovereign_cron.sh). A missing key raises immediately
with a message saying where to put it — no silent fallbacks.
"""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).parent
RESULTS_DIR = BASE_DIR / "sovereign_results"
DATA_DIR = BASE_DIR / "congress_data"
STATE_DIR = BASE_DIR / "sovereign_state"
for d in (RESULTS_DIR, DATA_DIR, STATE_DIR):
    d.mkdir(exist_ok=True)


class MissingKeyError(RuntimeError):
    pass


def require_env(name: str, hint: str = "") -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise MissingKeyError(
            f"{name} not set. {hint or 'Export it or add it to .envrc / ~/.coalition/secrets.env'}"
        )
    return val


def alpaca_keys() -> tuple[str, str, bool]:
    """(key, secret, paper). Paper defaults True — live requires explicit opt-in."""
    key = require_env("ALPACA_API_KEY", f"Add to {BASE_DIR / '.envrc'}")
    secret = require_env("ALPACA_SECRET_KEY", f"Add to {BASE_DIR / '.envrc'}")
    paper = os.environ.get("ALPACA_PAPER", "true").lower() == "true"
    return key, secret, paper


def anthropic_key() -> str:
    return require_env("ANTHROPIC_API_KEY", "Add to ~/.coalition/secrets.env")


def fred_key() -> str:
    return require_env("FRED_API_KEY", f"Add to {BASE_DIR / '.envrc'}")


# --- Model choices (one place to bump) -------------------------------------
THESIS_MODEL = "claude-sonnet-5"          # thesis generation
EXTRACT_MODEL = "claude-haiku-4-5-20251001"  # cheap PDF extraction

# --- Risk parameters (fractions of account equity) -------------------------
RISK = {
    "max_position_pct": 0.25,        # hard cap on any single position
    "high_conviction_pct": 0.25,
    "medium_conviction_pct": 0.15,
    "low_conviction_pct": 0.10,
    "stop_loss_pct": 0.08,           # default stop distance below entry (FLOOR for ATR stops)
    "atr_multiplier": 2.0,           # ATR-based stop: entry - (atr_multiplier * ATR_14)
    "target_pct": 0.15,              # default take-profit
    "max_risk_per_trade_pct": 0.02,  # position_pct * stop_pct must stay under this
    "daily_loss_halt_pct": 0.05,     # circuit breaker
    "sector_cap_pct": 0.50,
    "cash_reserve_pct": 0.10,
    "min_notional_usd": 1.00,        # Alpaca fractional minimum
    "max_positions": 6,
    "correlation_penalty_threshold": 0.70,  # avg corr with book above this → halve size
    "trail_trigger_pct": 0.10,       # +10% unrealized → raise stop to breakeven
}

# --- Signal weights (composite score) ---------------------------------------
SIGNAL_WEIGHTS = {
    "congress": 0.30,     # our alpha — weighted by member track record
    "momentum": 0.20,     # RSI / trend / volume technicals
    "sector": 0.15,       # sector rotation flow
    "options_flow": 0.12, # unusual options activity proxy
    "sentiment": 0.10,    # news velocity + headline tone
    "corr_break": 0.08,   # correlation divergence events
    "earnings": 0.05,     # earnings-proximity whisper (mostly a risk flag)
}

# Raised medium from 0.28 to 0.35 on 2026-07-28: at 0.28, marginal signals
# (0.285-0.29) got full 15% sizing. most losses were medium conviction
# entries that would have been blocked or downsized at 0.35.
CONVICTION_THRESHOLDS = {"high": 0.45, "medium": 0.35, "low": 0.15}

# --- AI IPO wave watchlist ---------------------------------------------------
# Recent AI-adjacent IPOs. `lockup` = approximate 180-day lockup expiry
# (supply overhang risk near that date). Edit as new listings appear.
AI_IPO_WATCHLIST = {
    "CRWV": {"name": "CoreWeave", "ipo": "2025-03-28", "theme": "AI cloud/GPU"},
    "ALAB": {"name": "Astera Labs", "ipo": "2024-03-20", "theme": "AI interconnect"},
    "RDDT": {"name": "Reddit", "ipo": "2024-03-21", "theme": "AI training data"},
    "TEM":  {"name": "Tempus AI", "ipo": "2024-06-14", "theme": "AI healthcare"},
    "ARM":  {"name": "Arm Holdings", "ipo": "2023-09-14", "theme": "AI silicon IP"},
    "RBRK": {"name": "Rubrik", "ipo": "2024-04-25", "theme": "data security/AI"},
}


def with_timeout(client, seconds: float = 30.0):
    """Give an alpaca-py client an HTTP timeout. alpaca-py sets none, so a
    stalled socket can hang a run forever -- and a run that hangs while holding
    the execute/manage lock silently disables every stop behind it."""
    import functools
    try:
        import requests
        session = getattr(client, "_session", None)
        if isinstance(session, requests.Session) and not getattr(session, "_sovereign_timeout", False):
            session.request = functools.partial(session.request, timeout=seconds)
            session._sovereign_timeout = True
    except Exception:   # a safety helper must never break the call it protects
        pass
    return client


def clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def load_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


def save_json(path: Path, obj):
    """Atomic: write a temp file beside the target, fsync, rename over it.
    A plain write_text truncates first, so a kill or a full disk mid-write
    left an empty positions file -- which read back as "no positions" and
    re-based every stop 8% under the current price."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "w") as f:
        f.write(json.dumps(obj, indent=2, default=str))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def cached_daily(path: Path, max_age_hours: float = 20.0):
    """Return cached JSON if fresh enough, else None."""
    data = load_json(path)
    if not data or "timestamp" not in data:
        return None
    try:
        ts = datetime.fromisoformat(data["timestamp"])
    except ValueError:
        return None
    if datetime.now() - ts > timedelta(hours=max_age_hours):
        return None
    return data
