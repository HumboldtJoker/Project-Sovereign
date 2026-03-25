"""
Centralized configuration for Sovereign Market Intelligence Agent.

All environment variables, API keys, and tunable parameters in one place.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ── Paths ────────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
LOGS_DIR = PROJECT_ROOT / "logs"

# ── API Keys ─────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
FRED_API_KEY = os.getenv("FRED_API_KEY", "")
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "")
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_PAPER = os.getenv("ALPACA_PAPER", "true").lower() in ("true", "1", "yes")

# ── Blockchain / Web3 ───────────────────────────────────────────────────────
OPERATOR_WALLET = os.getenv("OPERATOR_WALLET", "")
OPERATOR_PRIVATE_KEY = os.getenv("OPERATOR_PRIVATE_KEY", "")
BASE_RPC_URL = os.getenv("BASE_RPC_URL", "https://mainnet.base.org")
ERC8004_IDENTITY_REGISTRY = "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432"
ERC8004_REPUTATION_REGISTRY = os.getenv(
    "ERC8004_REPUTATION_REGISTRY",
    "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432",   # same contract unless separate deploy
)
ERC8004_AGENT_ID = os.getenv("ERC8004_AGENT_ID", "")

# ── Storacha ─────────────────────────────────────────────────────────────────
STORACHA_SPACE = os.getenv("STORACHA_SPACE", "")

# ── Agent Parameters ─────────────────────────────────────────────────────────
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-5-20250929")
MAX_ITERATIONS = int(os.getenv("MAX_ITERATIONS", "12"))
MAX_ANALYSIS_TIME_SECONDS = 120
MAX_CONCURRENT_TOOLS = 5

# ── Risk Parameters ──────────────────────────────────────────────────────────
MAX_POSITION_PCT = 0.30          # 30% max single position
DEFAULT_STOP_LOSS_PCT = 0.08     # 8% stop loss
DAILY_LOSS_LIMIT_PCT = 0.05      # 5% daily circuit breaker
CASH_RESERVE_PCT = 0.10          # 10% minimum cash
VIX_ADAPTIVE_STOP = {
    "low": 0.10,      # VIX < 15
    "normal": 0.08,   # VIX 15-25
    "elevated": 0.06, # VIX 25-35
    "extreme": 0.05,  # VIX > 35
}

# ── Technical Indicator Thresholds ───────────────────────────────────────────
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
BOLLINGER_SQUEEZE_THRESHOLD = 0.10
BOLLINGER_EXPANSION_THRESHOLD = 0.20

# ── Macro Regime Thresholds ──────────────────────────────────────────────────
VIX_THRESHOLDS = {
    "low": 15,
    "normal": 20,
    "elevated": 25,
    "extreme": 35,
}
REGIME_POSITION_MODIFIERS = {
    "BULLISH": 1.0,
    "NEUTRAL": 0.75,
    "CAUTIOUS": 0.5,
    "BEARISH": 0.25,
    "CRITICAL": 0.0,
}

# ── S&P 500 Sector Benchmark (Q4 2024) ──────────────────────────────────────
SP500_SECTOR_WEIGHTS = {
    "Technology": 32.0,
    "Healthcare": 11.5,
    "Financials": 13.5,
    "Consumer Cyclical": 10.5,
    "Communication Services": 9.0,
    "Industrials": 8.5,
    "Consumer Defensive": 6.0,
    "Energy": 3.5,
    "Utilities": 2.5,
    "Real Estate": 2.0,
    "Basic Materials": 2.0,
}

# ── Ensure directories exist ─────────────────────────────────────────────────
DATA_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)
