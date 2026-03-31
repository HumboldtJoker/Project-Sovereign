#!/usr/bin/env python3
"""
Historical Market Regime Backfill
=================================
Seeds the knowledge graph with ~5 years of historical macro data, sector
performance, and curated market events so the Sovereign Market Intelligence
Agent has institutional memory from day one.

Idempotent — safe to run multiple times (all writes use upserts).

Usage:
    python -m memory.backfill          # from project root
    python memory/backfill.py          # direct execution
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Ensure project root is on sys.path so `core.*` / `memory.*` resolve when
# the script is invoked directly (python memory/backfill.py).
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(_PROJECT_ROOT / ".env")

import pandas as pd

from core.config import (
    FRED_API_KEY,
    VIX_THRESHOLDS,
    REGIME_POSITION_MODIFIERS,
)
from memory.kg_engine import (
    init_db,
    add_entity,
    add_relationship,
    record_event,
    record_regime_change,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("backfill")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LOOKBACK_YEARS = 5

FRED_SERIES = {
    "VIXCLS": "VIX",
    "T10Y2Y": "Yield Curve 10Y-2Y Spread",
    "BAMLH0A0HY2": "High Yield Credit Spread",
    "FEDFUNDS": "Fed Funds Rate",
    "UNRATE": "Unemployment Rate",
}

# Alternative FRED series IDs to try if the primary ones fail.
# BAMLH0A0HY2 vs BAMLH0A0HYM2 — both are BofA HY spreads, different freq.
FRED_SERIES_FALLBACKS = {
    "BAMLH0A0HY2": ["BAMLH0A0HYM2"],
}

SECTOR_ETFS: Dict[str, str] = {
    "SPY": "S&P 500",
    "XLK": "Technology",
    "XLV": "Healthcare",
    "XLE": "Energy",
    "XLF": "Financials",
    "XLU": "Utilities",
    "XLP": "Consumer Staples",
    "XLI": "Industrials",
}

# Mapping from sector name back to representative ETF
SECTOR_TO_ETF = {v: k for k, v in SECTOR_ETFS.items()}

# Threshold config (mirrors analysis/macro.py scoring logic)
VIX_LOW = VIX_THRESHOLDS["low"]         # 15
VIX_NORMAL = VIX_THRESHOLDS["normal"]   # 20
VIX_ELEVATED = VIX_THRESHOLDS["elevated"]  # 25
VIX_EXTREME = VIX_THRESHOLDS["extreme"]    # 35

CREDIT_SPREAD_NORMAL = 3.5
CREDIT_SPREAD_ELEVATED = 5.0
CREDIT_SPREAD_CRISIS = 7.0

# Regime score boundaries (same as MacroAgent.get_market_regime)
REGIME_BOUNDARIES: List[Tuple[float, str]] = [
    (7.0, "CRITICAL"),
    (5.0, "BEARISH"),
    (3.0, "CAUTIOUS"),
    (1.5, "NEUTRAL"),
    (0.0, "BULLISH"),
]

# ---------------------------------------------------------------------------
# Hardcoded major market events
# ---------------------------------------------------------------------------

MARKET_EVENTS: List[Dict[str, Any]] = [
    {
        "id": "EVT:2022-01-FED-HIKES",
        "date": "2022-01-26",
        "description": "Fed signals aggressive rate hikes to combat inflation",
        "sectors": ["Technology", "Growth"],
        "regime": "CAUTIOUS",
        "severity": 7.0,
    },
    {
        "id": "EVT:2022-06-VIX-SPIKE",
        "date": "2022-06-13",
        "description": "VIX spikes above 30, recession fears grip markets",
        "sectors": list(SECTOR_ETFS.values()),
        "regime": "BEARISH",
        "severity": 8.0,
    },
    {
        "id": "EVT:2022-10-YIELD-INVERSION",
        "date": "2022-10-20",
        "description": "Yield curve deeply inverted — strongest recession signal since 1980s",
        "sectors": ["Financials", "S&P 500"],
        "regime": "BEARISH",
        "severity": 7.5,
    },
    {
        "id": "EVT:2023-03-SVB-COLLAPSE",
        "date": "2023-03-10",
        "description": "Silicon Valley Bank collapse triggers regional banking crisis",
        "sectors": ["Financials"],
        "regime": "BEARISH",
        "severity": 8.5,
    },
    {
        "id": "EVT:2023-07-AI-BOOM",
        "date": "2023-07-01",
        "description": "AI boom begins — ChatGPT mass adoption drives Technology rally",
        "sectors": ["Technology"],
        "regime": "BULLISH",
        "severity": 5.0,
    },
    {
        "id": "EVT:2023-11-FED-PIVOT",
        "date": "2023-11-01",
        "description": "Fed signals pivot — markets rally on rate-cut expectations",
        "sectors": list(SECTOR_ETFS.values()),
        "regime": "BULLISH",
        "severity": 6.0,
    },
    {
        "id": "EVT:2024-08-YEN-CARRY-UNWIND",
        "date": "2024-08-05",
        "description": "Yen carry trade unwind — VIX spikes to 65, global sell-off",
        "sectors": list(SECTOR_ETFS.values()),
        "regime": "CRITICAL",
        "severity": 9.5,
    },
    {
        "id": "EVT:2024-11-US-ELECTION",
        "date": "2024-11-05",
        "description": "US presidential election — policy shifts expected for Energy, Financials, Defense",
        "sectors": ["Energy", "Financials"],
        "regime": "NEUTRAL",
        "severity": 6.5,
    },
    {
        "id": "EVT:2025-06-AI-INFRA-PEAK",
        "date": "2025-06-15",
        "description": "AI infrastructure buildout peaks — semiconductor capex cycle crests",
        "sectors": ["Technology"],
        "regime": "NEUTRAL",
        "severity": 5.5,
    },
    {
        "id": "EVT:2025-12-FED-HOLD",
        "date": "2025-12-17",
        "description": "Fed holds rates steady — markets settle into neutral regime",
        "sectors": list(SECTOR_ETFS.values()),
        "regime": "NEUTRAL",
        "severity": 3.0,
    },
    {
        "id": "EVT:2026-02-OPEC-WAR",
        "date": "2026-02-10",
        "description": "OPEC+ production war begins — oil prices plunge, Energy sector hit",
        "sectors": ["Energy"],
        "regime": "CAUTIOUS",
        "severity": 7.0,
    },
    {
        "id": "EVT:2026-03-TARIFF-ESCALATION",
        "date": "2026-03-15",
        "description": "Reciprocal tariff escalation — trade uncertainty pushes regime to CAUTIOUS",
        "sectors": ["Industrials", "Technology", "Consumer Staples"],
        "regime": "CAUTIOUS",
        "severity": 7.5,
    },
]


# =========================================================================
# Step 1 — Pull FRED historical data
# =========================================================================

def _fetch_fred_series(
    fred,
    series_id: str,
    start: datetime,
    end: datetime,
) -> Optional[pd.Series]:
    """Fetch a FRED series, trying fallback IDs if the primary fails."""
    ids_to_try = [series_id] + FRED_SERIES_FALLBACKS.get(series_id, [])
    for sid in ids_to_try:
        try:
            data = fred.get_series(sid, observation_start=start, observation_end=end)
            if data is not None and len(data) > 0:
                logger.info("  Fetched %s (%d observations)", sid, len(data))
                return data
        except Exception as exc:
            logger.warning("  Failed to fetch %s: %s", sid, exc)
    return None


def pull_fred_data() -> Dict[str, pd.Series]:
    """Download 5 years of FRED macro data and return as {series_id: pd.Series}."""
    try:
        from fredapi import Fred
    except ImportError:
        logger.error("fredapi not installed. Run: pip install fredapi")
        return {}

    if not FRED_API_KEY:
        logger.error("FRED_API_KEY not set — cannot pull macro data")
        return {}

    fred = Fred(api_key=FRED_API_KEY)
    end = datetime.now()
    start = end - timedelta(days=365 * LOOKBACK_YEARS)

    data: Dict[str, pd.Series] = {}
    for series_id, label in FRED_SERIES.items():
        print(f"  Pulling {label} ({series_id})...")
        result = _fetch_fred_series(fred, series_id, start, end)
        if result is not None:
            data[series_id] = result
        else:
            logger.warning("  Skipping %s — no data returned", series_id)

    return data


# =========================================================================
# Step 2 — Classify historical regimes by month
# =========================================================================

def _score_month(row: Dict[str, Optional[float]]) -> Tuple[float, str]:
    """
    Compute a risk score for one month's averaged indicators and map to regime.

    Scoring mirrors analysis/macro.py MacroAgent.get_market_regime():
      VIX risk:          0-3 points
      Yield curve risk:  0-3 points
      Credit risk:       0-3 points
      Fed risk:          0-1 point
      Unemployment risk: 0-1 point
    """
    score = 0.0

    # --- VIX ---
    vix = row.get("VIXCLS")
    if vix is not None:
        if vix >= VIX_EXTREME:
            score += 3
        elif vix >= VIX_ELEVATED:
            score += 2
        elif vix >= VIX_NORMAL:
            score += 1
        elif vix < VIX_LOW:
            score += 0.5  # complacency risk

    # --- Yield curve ---
    yc = row.get("T10Y2Y")
    if yc is not None:
        if yc < -0.5:
            score += 3
        elif yc < 0:
            score += 2
        elif yc < 0.5:
            score += 1

    # --- Credit spread ---
    cs = row.get("BAMLH0A0HY2")
    if cs is not None:
        if cs >= CREDIT_SPREAD_CRISIS:
            score += 3
        elif cs >= CREDIT_SPREAD_ELEVATED:
            score += 2

    # --- Fed funds ---
    ff = row.get("FEDFUNDS")
    if ff is not None and ff >= 5.0:
        score += 1

    # --- Unemployment ---
    ur = row.get("UNRATE")
    if ur is not None and ur >= 5.5:
        score += 1

    # Map to regime label
    regime = "BULLISH"
    for threshold, label in REGIME_BOUNDARIES:
        if score >= threshold:
            regime = label
            break

    return round(score, 2), regime


def classify_regimes(
    fred_data: Dict[str, pd.Series],
) -> List[Dict[str, Any]]:
    """
    Resample all FRED series to monthly, score each month, and return a
    list of regime periods (consecutive months with the same regime label
    are merged into a single period).
    """
    if not fred_data:
        return []

    # Build a monthly DataFrame
    monthly_frames = {}
    for sid, series in fred_data.items():
        monthly = series.dropna().resample("MS").mean()
        monthly_frames[sid] = monthly

    combined = pd.DataFrame(monthly_frames)
    combined = combined.sort_index()

    # Score each month
    records = []
    for month_start, row in combined.iterrows():
        indicators = {k: (round(float(v), 4) if pd.notna(v) else None) for k, v in row.items()}
        score, regime = _score_month(indicators)
        records.append({
            "month": month_start,
            "regime": regime,
            "risk_score": score,
            "indicators": indicators,
        })

    # Merge consecutive months with same regime into periods
    if not records:
        return []

    periods: List[Dict[str, Any]] = []
    current = records[0].copy()
    current["start"] = current["month"]
    current["end"] = current["month"]

    for rec in records[1:]:
        if rec["regime"] == current["regime"]:
            current["end"] = rec["month"]
            # Keep running average of risk score
            current["risk_score"] = round(
                (current["risk_score"] + rec["risk_score"]) / 2, 2
            )
            # Merge indicators (keep latest)
            current["indicators"] = rec["indicators"]
        else:
            periods.append(current)
            current = rec.copy()
            current["start"] = rec["month"]
            current["end"] = rec["month"]

    periods.append(current)  # last period
    return periods


# =========================================================================
# Step 3 — Pull sector ETF performance per regime period
# =========================================================================

def pull_sector_returns(
    periods: List[Dict[str, Any]],
) -> Dict[str, Dict[str, Optional[float]]]:
    """
    For each regime period, download sector ETF data and compute the
    holding-period return.  Returns {period_key: {ETF: return_pct}}.
    """
    try:
        import yfinance as yf
    except ImportError:
        logger.error("yfinance not installed. Run: pip install yfinance")
        return {}

    if not periods:
        return {}

    # Figure out global date range to minimise downloads
    global_start = min(p["start"] for p in periods)
    global_end = max(p["end"] for p in periods) + timedelta(days=35)
    tickers = list(SECTOR_ETFS.keys())

    print(f"  Downloading {len(tickers)} ETFs from {global_start.date()} to {global_end.date()}...")
    try:
        prices = yf.download(
            tickers,
            start=global_start.strftime("%Y-%m-%d"),
            end=global_end.strftime("%Y-%m-%d"),
            auto_adjust=True,
            progress=False,
        )
    except Exception as exc:
        logger.error("yfinance download failed: %s", exc)
        return {}

    # Handle both single-ticker and multi-ticker DataFrame shapes
    if isinstance(prices.columns, pd.MultiIndex):
        close = prices["Close"] if "Close" in prices.columns.get_level_values(0) else prices
    else:
        close = prices

    results: Dict[str, Dict[str, Optional[float]]] = {}
    for period in periods:
        key = f"{period['regime']}:{period['start'].strftime('%Y-%m')}"
        p_start = period["start"]
        p_end = period["end"] + timedelta(days=31)  # include full last month

        mask = (close.index >= p_start) & (close.index <= p_end)
        window = close.loc[mask]

        returns: Dict[str, Optional[float]] = {}
        for ticker in tickers:
            try:
                col = window[ticker].dropna() if ticker in window.columns else pd.Series(dtype=float)
                if len(col) >= 2:
                    ret = (col.iloc[-1] / col.iloc[0] - 1) * 100
                    returns[ticker] = round(float(ret), 2)
                else:
                    returns[ticker] = None
            except Exception:
                returns[ticker] = None

        results[key] = returns

    return results


# =========================================================================
# Step 4 — Seed entities, relationships, events, and regimes
# =========================================================================

def seed_sector_entities() -> None:
    """Create SECTOR and TICKER entities plus belongs_to edges."""
    print("[4a] Seeding sector & ticker entities...")
    for ticker, sector in SECTOR_ETFS.items():
        add_entity(ticker, "TICKER", {"sector": sector})
        add_entity(sector, "SECTOR")
        add_relationship(
            ticker, sector, "belongs_to",
            source_type="TICKER", target_type="SECTOR",
        )

    # Extra sectors for events that reference sectors without an ETF
    for extra in ("Growth", "Semiconductors"):
        add_entity(extra, "SECTOR")


def seed_events() -> None:
    """Insert hardcoded market events and build EVENT -> SECTOR impact edges."""
    print("[4b] Seeding market events...")
    for evt in MARKET_EVENTS:
        # record_event creates the event row, embeds it, and links mentioned
        # entities via triggered_by edges automatically.
        record_event(
            event_text=evt["description"],
            event_type="macro_event",
            entities=evt["sectors"],
            impact_score=evt.get("severity", 5.0),
            regime=evt.get("regime"),
            properties={
                "event_id": evt["id"],
                "event_date": evt["date"],
            },
        )
        # Ensure sector entities exist for every impacted sector
        for sector in evt["sectors"]:
            add_entity(sector, "SECTOR")

        # EVENT -> REGIME link
        if evt.get("regime"):
            add_entity(evt["regime"], "REGIME", {"source": "backfill"})

        print(f"    {evt['date']}  {evt['description'][:60]}")


def seed_regimes(
    periods: List[Dict[str, Any]],
    sector_returns: Dict[str, Dict[str, Optional[float]]],
) -> None:
    """Store regime periods and build SECTOR performed_during REGIME edges."""
    print("[4c] Seeding regime history & sector performance...")

    for period in periods:
        start_str = period["start"].strftime("%Y-%m-%d")
        end_str = period["end"].strftime("%Y-%m-%d")
        key = f"{period['regime']}:{period['start'].strftime('%Y-%m')}"
        returns = sector_returns.get(key, {})

        # Clean indicators dict: drop None values for JSON
        clean_indicators = {
            k: v for k, v in (period.get("indicators") or {}).items() if v is not None
        }

        # Record this regime period into market_regimes table.
        # record_regime_change() closes any open regime and opens a new one,
        # and also creates REGIME + INDICATOR entities with relationships.
        record_regime_change(
            new_regime=period["regime"],
            risk_score=period["risk_score"],
            indicators=clean_indicators,
        )

        # SECTOR performed_during REGIME edges
        for etf_ticker, ret_pct in returns.items():
            if ret_pct is not None:
                sector_name = SECTOR_ETFS.get(etf_ticker, etf_ticker)
                add_entity(etf_ticker, "TICKER", {"sector": sector_name})
                add_entity(period["regime"], "REGIME")
                add_relationship(
                    etf_ticker, period["regime"], "correlated_with",
                    weight=abs(ret_pct),
                    properties={
                        "period_start": start_str,
                        "period_end": end_str,
                        "return_pct": ret_pct,
                        "direction": "positive" if ret_pct > 0 else "negative",
                    },
                    source_type="TICKER",
                    target_type="REGIME",
                )

    print(f"    Stored {len(periods)} regime periods")


def _count_table(table_name: str) -> int:
    """Count rows in a table via the module-level connection."""
    from memory.kg_engine import _get_conn
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(f"SELECT COUNT(*) FROM {table_name}")  # noqa: S608
    count = cur.fetchone()[0]
    cur.close()
    return count


# =========================================================================
# Orchestrator
# =========================================================================

def main() -> None:
    """Run the full historical backfill pipeline."""
    banner = (
        "\n"
        "================================================================\n"
        "  Sovereign — Historical Backfill\n"
        "================================================================\n"
    )
    print(banner)

    # -- Initialise DB --
    print("[0] Initialising knowledge graph database...")
    result = init_db()
    if result.get("success"):
        print(f"    Tables: {result.get('tables', [])}")
    else:
        print(f"    WARNING: init_db returned: {result}")

    # -- Step 1: FRED data --
    print("\n[1] Pulling FRED historical data (5 years)...")
    fred_data = pull_fred_data()
    if fred_data:
        print(f"    Retrieved {len(fred_data)}/{len(FRED_SERIES)} series")
    else:
        print("    WARNING: No FRED data retrieved — regime classification will be skipped")

    # -- Step 2: Classify regimes --
    print("\n[2] Classifying historical market regimes by month...")
    periods = classify_regimes(fred_data)
    if periods:
        regime_counts: Dict[str, int] = {}
        for p in periods:
            regime_counts[p["regime"]] = regime_counts.get(p["regime"], 0) + 1
        print(f"    Identified {len(periods)} regime periods:")
        for regime, count in sorted(regime_counts.items()):
            print(f"      {regime}: {count} periods")
    else:
        print("    No regime periods classified (missing FRED data)")

    # -- Step 3: Sector ETF returns --
    print("\n[3] Pulling sector ETF performance per regime period...")
    sector_returns = pull_sector_returns(periods)
    if sector_returns:
        print(f"    Computed returns for {len(sector_returns)} periods")
    else:
        print("    No sector return data computed")

    # -- Step 4: Seed everything into KG --
    print("\n[4] Seeding knowledge graph...")
    seed_sector_entities()
    seed_events()
    seed_regimes(periods, sector_returns)

    # -- Summary --
    entity_count = _count_table("kg_entities")
    rel_count = _count_table("kg_relationships")
    event_count = _count_table("kg_events")
    regime_count = _count_table("market_regimes")
    decision_count = _count_table("agent_decisions")

    summary = (
        "\n"
        "================================================================\n"
        "  Backfill Complete\n"
        "================================================================\n"
        f"  Entities:       {entity_count}\n"
        f"  Relationships:  {rel_count}\n"
        f"  Events:         {event_count}\n"
        f"  Regime periods: {regime_count}\n"
        f"  Decisions:      {decision_count}\n"
        "================================================================\n"
    )
    print(summary)


if __name__ == "__main__":
    main()
