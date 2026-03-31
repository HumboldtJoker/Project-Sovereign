"""
Market Context Builder for the Sovereign.
===================================================================

Builds concise context briefings for the ReAct agent's system prompt by
querying the Knowledge Graph. Also provides the post-run enrichment pipeline
that extracts entities, events, and decisions from completed execution logs.
"""

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from memory.kg_engine import (
    add_entity,
    add_relationship,
    associative_query,
    get_regime_history,
    get_similar_conditions,
    record_decision,
    record_event,
    record_outcome,
    record_regime_change,
)

logger = logging.getLogger(__name__)


# ── Context builder ─────────────────────────────────────────────────────────

def build_market_context(
    current_regime: Optional[str] = None,
) -> str:
    """Build a concise historical context briefing for the agent's system prompt.

    Queries the KG for:
    - Current regime info and history
    - Similar past regimes and what happened
    - What worked / what didn't in similar conditions
    - Recent notable events

    Returns a formatted string, kept under ~500 tokens.
    """
    sections = []

    # 1. Current regime
    if current_regime:
        sections.append(f"CURRENT REGIME: {current_regime}")

        # Find similar past regimes
        regime_history = get_regime_history(regime_filter=current_regime, limit=5)
        if regime_history:
            past_entries = []
            for rh in regime_history[:3]:
                duration = rh.get("duration_hours")
                dur_str = f" ({duration:.0f}h)" if duration else ""
                risk_str = f" risk={rh['risk_score']:.1f}" if rh.get("risk_score") else ""
                past_entries.append(f"  - {rh['started_at'][:10]}{dur_str}{risk_str}")
            if past_entries:
                sections.append("Past occurrences of this regime:\n" + "\n".join(past_entries))

    # 2. Similar conditions (if we can infer current indicators)
    # Try to get the most recent regime's indicators as a proxy
    recent_regimes = get_regime_history(limit=1)
    if recent_regimes and recent_regimes[0].get("indicators"):
        current_indicators = recent_regimes[0]["indicators"]
        # Convert all values to float for comparison
        float_indicators = {}
        for k, v in current_indicators.items():
            try:
                float_indicators[k] = float(v)
            except (ValueError, TypeError):
                continue

        if float_indicators:
            similar = get_similar_conditions(float_indicators, n=3)
            if similar:
                sim_entries = []
                for s in similar:
                    sim_entries.append(
                        f"  - {s['regime']} ({s['started_at'][:10] if s.get('started_at') else '?'})"
                        f" sim={s['similarity']:.2f}"
                    )
                if sim_entries:
                    sections.append(
                        "Similar historical conditions:\n" + "\n".join(sim_entries)
                    )

    # 3. What worked / what didn't — query decisions with outcomes
    if current_regime:
        query = f"trading decisions during {current_regime} regime with outcomes"
    else:
        query = "recent trading decisions with outcomes"

    try:
        relevant = associative_query(query, n_results=6, current_regime=current_regime)
    except Exception as e:
        logger.warning("Associative query failed during context build: %s", e)
        relevant = []

    # Separate decisions with outcomes (what worked vs didn't)
    worked = []
    failed = []
    recent_events = []

    for item in relevant:
        if item["type"] == "decision" and item.get("outcome"):
            outcome = item["outcome"]
            summary = _outcome_summary(outcome)
            entry = (
                f"  - [{item.get('phase', '?')}] {item.get('action', '?')} "
                f"tickers={item.get('tickers', [])} "
                f"regime={item.get('regime_at_time', '?')}: {summary}"
            )
            if _is_positive_outcome(outcome):
                worked.append(entry)
            else:
                failed.append(entry)
        elif item["type"] == "event":
            recent_events.append(
                f"  - [{item.get('event_type', '?')}] "
                f"{item.get('event_text', '')[:100]} "
                f"impact={item.get('impact_score', 0):.1f}"
            )

    if worked:
        sections.append("What WORKED in similar conditions:\n" + "\n".join(worked[:3]))
    if failed:
        sections.append("What FAILED in similar conditions:\n" + "\n".join(failed[:3]))
    if recent_events:
        sections.append("Relevant recent events:\n" + "\n".join(recent_events[:3]))

    # 4. Assemble, enforcing token budget
    if not sections:
        return "No historical context available yet. This appears to be the first run."

    briefing = "\n\n".join(sections)

    # Rough token estimate: ~4 chars per token, budget ~500 tokens = ~2000 chars
    if len(briefing) > 2000:
        briefing = briefing[:1980] + "\n[...truncated]"

    return briefing


def _outcome_summary(outcome: Dict) -> str:
    """Extract a short summary from an outcome dict."""
    if isinstance(outcome, dict):
        if "summary" in outcome:
            return str(outcome["summary"])[:80]
        if "pnl" in outcome:
            pnl = outcome["pnl"]
            return f"PnL: {pnl}" if isinstance(pnl, (int, float)) else str(pnl)[:80]
        if "error" in outcome:
            return f"ERROR: {str(outcome['error'])[:60]}"
        # Fallback: first key-value
        for k, v in outcome.items():
            return f"{k}: {str(v)[:60]}"
    return str(outcome)[:80]


def _is_positive_outcome(outcome: Dict) -> bool:
    """Heuristic: determine if an outcome was positive."""
    if not isinstance(outcome, dict):
        return False

    # Check for explicit PnL
    pnl = outcome.get("pnl", outcome.get("profit", outcome.get("return")))
    if pnl is not None:
        try:
            return float(pnl) > 0
        except (ValueError, TypeError):
            pass

    # Check for success flag
    if "success" in outcome:
        return bool(outcome["success"])

    # Check for error indicators
    if "error" in outcome or "failure" in outcome:
        return False

    # Default: assume neutral
    return True


# ── Post-run enrichment ─────────────────────────────────────────────────────

def enrich_from_run(execution_log: Dict[str, Any]) -> Dict[str, Any]:
    """Extract entities, events, and decisions from a completed execution log
    and store them in the KG.

    Expected execution_log structure (from DecisionLoop._build_log()):
    {
        "session_id": str,
        "timestamp_start": str,
        "timestamp_end": str,
        "decisions": [
            {
                "step": int,
                "phase": str,   # discover, plan, execute, verify
                "action": str,
                "reasoning": str,
                "tools_called": [str],
                "result": str,
                "timestamp": str,
                "safety_checks": [str] (optional),
            },
            ...
        ],
        "retries": [...],
        "failures": [...],
        "final_output": {...},
    }

    Returns enrichment stats.
    """
    session_id = execution_log.get("session_id", "unknown")
    decisions_raw = execution_log.get("decisions", [])
    failures = execution_log.get("failures", [])
    final_output = execution_log.get("final_output") or {}

    stats = {
        "session_id": session_id,
        "entities_created": 0,
        "relationships_created": 0,
        "decisions_stored": 0,
        "events_stored": 0,
        "regime_recorded": False,
    }

    # Detect the regime from decision results
    detected_regime = _extract_regime(decisions_raw)
    if detected_regime:
        try:
            indicators = _extract_indicators(decisions_raw)
            record_regime_change(
                detected_regime,
                risk_score=_estimate_risk_score(detected_regime),
                indicators=indicators,
            )
            stats["regime_recorded"] = True
        except Exception as e:
            logger.warning("Failed to record regime: %s", e)

    # Process each decision step
    for dec in decisions_raw:
        phase = dec.get("phase", "unknown")
        action = dec.get("action", "unknown")
        reasoning = dec.get("reasoning", "")
        result = dec.get("result", "")

        # Extract tickers from the result text
        tickers = _extract_tickers(result + " " + reasoning)

        # Store decision
        try:
            dec_result = record_decision(
                session_id=session_id,
                phase=phase,
                action=action,
                tickers=tickers,
                reasoning=reasoning[:500],
                regime=detected_regime,
            )
            stats["decisions_stored"] += 1

            # If this is the final step with output, record it as an outcome
            if phase in ("execute", "verify") and final_output:
                try:
                    record_outcome(dec_result["decision_id"], final_output)
                except Exception as e:
                    logger.warning("Failed to record outcome: %s", e)

        except Exception as e:
            logger.warning("Failed to record decision (phase=%s): %s", phase, e)

        # Extract and store entities from reasoning and result
        entities_found = _extract_entities_from_text(reasoning + " " + result)
        for ent_name, ent_type in entities_found:
            try:
                res = add_entity(ent_name, ent_type)
                if res.get("created"):
                    stats["entities_created"] += 1
            except Exception as e:
                logger.warning("Failed to add entity %s: %s", ent_name, e)

        # Create relationships between tickers and detected sectors
        for ticker in tickers:
            sector = _guess_sector(ticker)
            if sector:
                try:
                    res = add_relationship(
                        ticker, sector, "belongs_to",
                        source_type="TICKER", target_type="SECTOR",
                    )
                    if res.get("created"):
                        stats["relationships_created"] += 1
                except Exception as e:
                    logger.warning("Failed to add relationship: %s", e)

        # Store notable events from tools results
        if dec.get("tools_called"):
            event_text = f"{phase}/{action}: {result[:200]}"
            impact = _estimate_impact(result)
            if impact > 0.3:  # Only store notable events
                try:
                    record_event(
                        event_text=event_text,
                        event_type=phase,
                        entities=tickers,
                        impact_score=impact,
                        regime=detected_regime,
                        properties={"tools": dec.get("tools_called", [])},
                    )
                    stats["events_stored"] += 1
                except Exception as e:
                    logger.warning("Failed to record event: %s", e)

    # Record failures as events
    for fail in failures:
        try:
            record_event(
                event_text=f"Agent failure: {fail.get('reason', 'unknown')} in phase {fail.get('phase', '?')}",
                event_type="agent_failure",
                entities=[],
                impact_score=0.8,
                regime=detected_regime,
            )
            stats["events_stored"] += 1
        except Exception as e:
            logger.warning("Failed to record failure event: %s", e)

    logger.info(
        "Enrichment complete for session %s: %d decisions, %d events, %d entities",
        session_id, stats["decisions_stored"], stats["events_stored"],
        stats["entities_created"],
    )

    return stats


# ── Text extraction helpers ─────────────────────────────────────────────────

# Common ticker pattern: 1-5 uppercase letters, often in context of trading
_TICKER_RE = re.compile(r'\b([A-Z]{1,5})\b')

# Words that look like tickers but aren't
_TICKER_STOPWORDS = frozenset([
    "A", "I", "AM", "AN", "AS", "AT", "BE", "BY", "DO", "GO",
    "IF", "IN", "IS", "IT", "ME", "MY", "NO", "OF", "OK", "ON",
    "OR", "SO", "TO", "UP", "US", "WE", "BUY", "SELL", "ALL",
    "AND", "ARE", "BUT", "CAN", "FOR", "GET", "GOT", "HAS", "HAD",
    "HER", "HIM", "HIS", "HOW", "ITS", "LET", "MAY", "NEW", "NOT",
    "NOW", "OLD", "OUR", "OUT", "OWN", "RUN", "SAY", "SHE", "THE",
    "TOO", "TRY", "USE", "WAY", "WHO", "WHY", "YES", "YET", "YOU",
    "STEP", "TASK", "JSON", "TRUE", "FALSE", "NULL", "NONE", "WITH",
    "FROM", "THIS", "THAT", "THEY", "THEM", "WHAT", "WHEN", "WILL",
    "YOUR", "HAVE", "EACH", "MAKE", "LIKE", "JUST", "OVER", "SUCH",
    "TAKE", "THAN", "VERY", "SOME", "BEEN", "CALL", "COME", "DONE",
    "FIND", "GIVE", "GOOD", "HERE", "HIGH", "KEEP", "LAST", "LONG",
    "LOOK", "MANY", "MUCH", "MUST", "NAME", "NEXT", "ONLY", "PART",
    "RISK", "SAME", "STOP", "THEN", "TURN", "USED", "WANT", "WELL",
    "WORK", "YEAR", "ALSO", "BACK", "EVEN", "HELP", "INTO", "MOST",
    "BOTH", "DOWN", "HOLD", "AFTER", "AGAIN", "BEING", "BELOW",
    "ERROR", "PHASE", "ACTION", "FINAL", "ANSWER", "ORDER",
    "TRADE", "PRICE", "STOCK", "VALUE", "TOTAL", "LIMIT",
    "CHECK", "ABORT", "PAPER", "LIVE", "MODE", "SCORE",
    "ETF", "EPS", "GDP", "IPO", "SEC", "FED", "CPI", "PPI",
    "PMI", "PCE", "YOY", "MOM", "USD", "EUR", "GBP", "JPY",
])

# Known ETF/Ticker patterns for higher confidence
_KNOWN_TICKERS = frozenset([
    "SPY", "QQQ", "IWM", "DIA", "VOO", "VTI", "AAPL", "MSFT",
    "GOOGL", "GOOG", "AMZN", "NVDA", "META", "TSLA", "BRK",
    "UNH", "JNJ", "JPM", "V", "PG", "XOM", "HD", "CVX", "MA",
    "ABBV", "MRK", "AVGO", "PEP", "KO", "COST", "TMO", "MCD",
    "WMT", "CSCO", "ACN", "ABT", "DHR", "VZ", "ADBE", "CRM",
    "NKE", "CMCSA", "TXN", "NEE", "BMY", "INTC", "AMD", "QCOM",
    "XLK", "XLF", "XLV", "XLE", "XLI", "XLY", "XLP", "XLU",
    "XLB", "XLRE", "XLC", "VIX", "TNX",
])


def _extract_tickers(text: str) -> List[str]:
    """Extract likely stock tickers from text."""
    matches = _TICKER_RE.findall(text)
    tickers = []
    seen = set()
    for m in matches:
        if m not in seen and (m in _KNOWN_TICKERS or (m not in _TICKER_STOPWORDS and len(m) >= 2)):
            tickers.append(m)
            seen.add(m)
    return tickers[:10]  # Cap at 10


def _extract_entities_from_text(text: str) -> List[tuple]:
    """Extract entity (name, type) pairs from text."""
    entities = []

    # Tickers
    for t in _extract_tickers(text):
        entities.append((t, "TICKER"))

    # Sectors
    sector_keywords = {
        "technology": "Technology", "tech": "Technology",
        "healthcare": "Healthcare", "health": "Healthcare",
        "financials": "Financials", "financial": "Financials",
        "consumer cyclical": "Consumer Cyclical",
        "consumer defensive": "Consumer Defensive",
        "communication": "Communication Services",
        "industrials": "Industrials", "industrial": "Industrials",
        "energy": "Energy", "utilities": "Utilities",
        "real estate": "Real Estate", "materials": "Basic Materials",
    }
    lower = text.lower()
    for keyword, sector_name in sector_keywords.items():
        if keyword in lower:
            entities.append((sector_name, "SECTOR"))

    # Indicators
    indicator_keywords = ["VIX", "RSI", "MACD", "SMA", "EMA", "Bollinger"]
    for ind in indicator_keywords:
        if ind in text:
            entities.append((ind, "INDICATOR"))

    return entities


def _extract_regime(decisions: List[Dict]) -> Optional[str]:
    """Try to detect the regime from decision results."""
    regime_keywords = {
        "BULLISH": ["bullish", "bull market", "risk-on"],
        "NEUTRAL": ["neutral", "mixed signals", "sideways"],
        "CAUTIOUS": ["cautious", "elevated volatility", "uncertainty"],
        "BEARISH": ["bearish", "bear market", "risk-off", "downturn"],
        "CRITICAL": ["critical", "extreme volatility", "crisis", "crash"],
    }

    combined_text = " ".join(
        d.get("result", "") + " " + d.get("reasoning", "")
        for d in decisions
    ).lower()

    best_regime = None
    best_count = 0

    for regime, keywords in regime_keywords.items():
        count = sum(1 for kw in keywords if kw in combined_text)
        if count > best_count:
            best_count = count
            best_regime = regime

    return best_regime


def _extract_indicators(decisions: List[Dict]) -> Dict[str, Any]:
    """Try to extract indicator values from decision results."""
    indicators = {}
    combined_text = " ".join(d.get("result", "") for d in decisions)

    # VIX pattern
    vix_match = re.search(r'VIX[:\s]+(\d+\.?\d*)', combined_text, re.IGNORECASE)
    if vix_match:
        indicators["vix"] = float(vix_match.group(1))

    # RSI pattern
    rsi_match = re.search(r'RSI[:\s]+(\d+\.?\d*)', combined_text, re.IGNORECASE)
    if rsi_match:
        indicators["rsi"] = float(rsi_match.group(1))

    # Yield pattern
    yield_match = re.search(r'(?:10Y|yield)[:\s]+(\d+\.?\d*)', combined_text, re.IGNORECASE)
    if yield_match:
        indicators["yield_10y"] = float(yield_match.group(1))

    return indicators


def _estimate_risk_score(regime: str) -> float:
    """Map regime name to a rough risk score."""
    scores = {
        "BULLISH": 0.2,
        "NEUTRAL": 0.4,
        "CAUTIOUS": 0.6,
        "BEARISH": 0.8,
        "CRITICAL": 1.0,
    }
    return scores.get(regime, 0.5)


def _estimate_impact(result_text: str) -> float:
    """Estimate event impact from result text. Returns 0.0-1.0."""
    high_impact_words = [
        "crash", "surge", "extreme", "crisis", "halt", "circuit breaker",
        "war", "pandemic", "default", "bankruptcy", "fed rate",
    ]
    medium_impact_words = [
        "earnings", "guidance", "upgrade", "downgrade", "volatility",
        "breakout", "breakdown", "divergence", "reversal",
    ]

    lower = result_text.lower()
    score = 0.0

    for word in high_impact_words:
        if word in lower:
            score += 0.3

    for word in medium_impact_words:
        if word in lower:
            score += 0.15

    return min(1.0, score)


_SECTOR_MAP = {
    "AAPL": "Technology", "MSFT": "Technology", "NVDA": "Technology",
    "GOOGL": "Technology", "GOOG": "Technology", "META": "Technology",
    "AMZN": "Consumer Cyclical", "TSLA": "Consumer Cyclical",
    "AVGO": "Technology", "ADBE": "Technology", "CRM": "Technology",
    "CSCO": "Technology", "INTC": "Technology", "AMD": "Technology",
    "QCOM": "Technology", "TXN": "Technology", "ACN": "Technology",
    "JPM": "Financials", "V": "Financials", "MA": "Financials",
    "BAC": "Financials", "GS": "Financials", "MS": "Financials",
    "UNH": "Healthcare", "JNJ": "Healthcare", "ABBV": "Healthcare",
    "MRK": "Healthcare", "TMO": "Healthcare", "ABT": "Healthcare",
    "PFE": "Healthcare", "LLY": "Healthcare",
    "XOM": "Energy", "CVX": "Energy", "COP": "Energy",
    "PG": "Consumer Defensive", "KO": "Consumer Defensive",
    "PEP": "Consumer Defensive", "COST": "Consumer Defensive",
    "WMT": "Consumer Defensive", "MCD": "Consumer Defensive",
    "HD": "Consumer Cyclical", "NKE": "Consumer Cyclical",
    "DIS": "Communication Services", "CMCSA": "Communication Services",
    "NFLX": "Communication Services", "T": "Communication Services",
    "VZ": "Communication Services",
    "NEE": "Utilities", "DUK": "Utilities", "SO": "Utilities",
    "UNP": "Industrials", "HON": "Industrials", "CAT": "Industrials",
    "BA": "Industrials", "GE": "Industrials", "RTX": "Industrials",
    "AMT": "Real Estate", "PLD": "Real Estate",
    "LIN": "Basic Materials", "APD": "Basic Materials",
    # Sector ETFs
    "XLK": "Technology", "XLF": "Financials", "XLV": "Healthcare",
    "XLE": "Energy", "XLI": "Industrials", "XLY": "Consumer Cyclical",
    "XLP": "Consumer Defensive", "XLU": "Utilities",
    "XLB": "Basic Materials", "XLRE": "Real Estate",
    "XLC": "Communication Services",
}


def _guess_sector(ticker: str) -> Optional[str]:
    """Map a ticker to its sector, if known."""
    return _SECTOR_MAP.get(ticker)
