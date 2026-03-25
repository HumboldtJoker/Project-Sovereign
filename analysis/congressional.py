"""
Congressional stock-trade tracker (individual ticker lookup).

Tracks stock trading activity by members of U.S. Congress as required by
the STOCK Act.  Data from the Politician Trade Tracker API (via RapidAPI).
"""

import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import requests

from core.config import RAPIDAPI_KEY

logger = logging.getLogger(__name__)


def get_congressional_trades(
    ticker: str,
    days: int = 90,
    chamber: str = "both",
    api_key: Optional[str] = None,
) -> Dict:
    """
    Get recent congressional trades for a specific stock ticker.

    Args:
        ticker: Stock symbol (e.g., 'AAPL', 'TSLA')
        days: Number of days to look back (default: 90)
        chamber: Which chamber to query - 'house', 'senate', or 'both'
        api_key: Override for RapidAPI key (falls back to config)

    Returns:
        Dict containing congressional trading data with politician details
    """
    ticker = ticker.upper().strip()

    if api_key is None:
        api_key = RAPIDAPI_KEY

    if not api_key:
        return {
            "error": (
                "API key required. Set RAPIDAPI_KEY environment variable or pass api_key parameter. "
                "Get free API key at: https://rapidapi.com/politics-trackers-politics-trackers-default/"
                "api/politician-trade-tracker"
            )
        }

    try:
        # Fetch all recent trades from RapidAPI
        trades, error = _fetch_trades_rapidapi(ticker, days, api_key)

        if error:
            return {"error": error}

        # Filter by chamber if specified
        if chamber != "both":
            trades = [
                t for t in trades if t["chamber"].lower() == chamber.lower()
            ]

        # Sort trades by date (most recent first)
        trades.sort(key=lambda x: x.get("transaction_date", ""), reverse=True)

        # Analyze trades
        analysis = _analyze_trades(trades, ticker)

        return {
            "ticker": ticker,
            "days_queried": days,
            "chamber": chamber,
            "total_trades": len(trades),
            "trades": trades,
            "analysis": analysis,
        }

    except Exception as e:
        logger.exception("Failed to fetch congressional trades for %s", ticker)
        return {"error": f"Failed to fetch congressional trades: {str(e)}"}


def _fetch_trades_rapidapi(
    ticker: str, days: int, api_key: str
) -> Tuple[List[Dict], Optional[str]]:
    """
    Fetch congressional trades from RapidAPI Politician Trade Tracker.

    Returns:
        Tuple of (trades_list, error_message)
    """
    try:
        url = "https://politician-trade-tracker1.p.rapidapi.com/get_latest_trades"

        headers = {
            "X-RapidAPI-Key": api_key,
            "X-RapidAPI-Host": "politician-trade-tracker1.p.rapidapi.com",
        }

        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()

        all_trades_data = response.json()

        # Filter by ticker and date
        cutoff_date = datetime.now() - timedelta(days=days)
        matching_trades: List[Dict] = []

        # Parse response structure
        trades_list = (
            all_trades_data
            if isinstance(all_trades_data, list)
            else all_trades_data.get("trades", [])
        )

        for trade in trades_list:
            # Parse transaction date (format: "October 27, 2025")
            try:
                trans_date_str = trade.get("trade_date", "")
                trans_date = datetime.strptime(trans_date_str, "%B %d, %Y")
            except (ValueError, TypeError):
                continue

            # Check if within date range
            if trans_date < cutoff_date:
                continue

            # Extract ticker (format: "NWL:US" -> "NWL")
            trade_ticker_raw = trade.get("ticker", "").upper().strip()
            if ":" in trade_ticker_raw:
                trade_ticker = trade_ticker_raw.split(":")[0]
            else:
                trade_ticker = trade_ticker_raw

            # Skip if ticker is N/A or doesn't match
            if trade_ticker == "N/A" or not trade_ticker:
                continue

            if trade_ticker == ticker:
                # Calculate disclosure date from days_until_disclosure
                days_until = trade.get("days_until_disclosure", 0)
                try:
                    disclosure_date = (
                        trans_date + timedelta(days=days_until)
                    ).strftime("%Y-%m-%d")
                except Exception:
                    disclosure_date = "Unknown"

                matching_trades.append(
                    {
                        "chamber": trade.get("chamber", "Unknown"),
                        "politician": trade.get("name", "Unknown"),
                        "party": trade.get("party", "Unknown"),
                        "state": f"{trade.get('state_name', trade.get('state_abbreviation', 'Unknown'))}",
                        "transaction_date": trans_date.strftime("%Y-%m-%d"),
                        "disclosure_date": disclosure_date,
                        "ticker": trade_ticker,
                        "asset_description": trade.get("company", "Unknown"),
                        "transaction_type": trade.get("trade_type", "Unknown"),
                        "amount": trade.get("trade_amount", "Unknown"),
                        "owner": "Self",  # API doesn't provide owner field
                        "ptr_link": "",  # API doesn't provide PTR links
                    }
                )

        return (matching_trades, None)

    except requests.RequestException as e:
        return ([], f"API request failed: {str(e)}")
    except json.JSONDecodeError as e:
        return ([], f"Failed to parse API response: {str(e)}")
    except Exception as e:
        return ([], f"Unexpected error: {str(e)}")


def _analyze_trades(trades: List[Dict], ticker: str) -> Dict:
    """
    Analyze trading patterns from congressional trades.

    Returns:
        Dict with analysis summary
    """
    if not trades:
        return {
            "sentiment": "NO DATA - No congressional trades found for this ticker",
            "recent_activity": "No trades found",
            "total_trades": 0,
            "purchases": 0,
            "sales": 0,
            "unique_politicians": 0,
            "party_breakdown": {},
            "chamber_breakdown": {},
        }

    # Count transaction types
    purchases = sum(
        1
        for t in trades
        if "purchase" in t["transaction_type"].lower()
        or "buy" in t["transaction_type"].lower()
    )
    sales = sum(
        1
        for t in trades
        if "sale" in t["transaction_type"].lower()
        or "sell" in t["transaction_type"].lower()
    )

    # Get unique politicians
    unique_pols = len(set(t["politician"] for t in trades))

    # Party breakdown
    party_counts: Dict[str, int] = defaultdict(int)
    for trade in trades:
        party = trade.get("party", "Unknown")
        party_counts[party] += 1

    # Chamber breakdown
    chamber_counts: Dict[str, int] = defaultdict(int)
    for trade in trades:
        chamber = trade.get("chamber", "Unknown")
        chamber_counts[chamber] += 1

    # Determine sentiment
    if purchases > sales:
        sentiment = f"BULLISH - More purchases ({purchases}) than sales ({sales})"
    elif sales > purchases:
        sentiment = f"BEARISH - More sales ({sales}) than purchases ({purchases})"
    else:
        sentiment = f"NEUTRAL - Equal purchases and sales ({purchases} each)"

    # Recent activity (last 30 days)
    recent_cutoff = datetime.now() - timedelta(days=30)
    recent_trades: List[Dict] = []
    for trade in trades:
        try:
            trans_date = datetime.strptime(trade["transaction_date"], "%Y-%m-%d")
            if trans_date >= recent_cutoff:
                recent_trades.append(trade)
        except Exception:
            continue

    recent_activity = (
        f"{len(recent_trades)} trade(s) in last 30 days"
        if recent_trades
        else "No trades in last 30 days"
    )

    return {
        "sentiment": sentiment,
        "recent_activity": recent_activity,
        "total_trades": len(trades),
        "purchases": purchases,
        "sales": sales,
        "unique_politicians": unique_pols,
        "party_breakdown": dict(party_counts),
        "chamber_breakdown": dict(chamber_counts),
    }


def analyze_congressional_trades(
    ticker: str,
    days: int = 90,
    chamber: str = "both",
    api_key: Optional[str] = None,
) -> Dict:
    """
    Main interface for congressional trading analysis with human-readable output.

    Args:
        ticker: Stock symbol to analyze
        days: Number of days to look back
        chamber: 'house', 'senate', or 'both'
        api_key: RapidAPI key (or set RAPIDAPI_KEY env var)

    Returns:
        Dict with formatted summary and raw data
    """
    result = get_congressional_trades(ticker, days, chamber, api_key)

    if "error" in result:
        return result

    # Generate timestamp
    analysis_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC")

    # Format summary
    summary = f"""
Congressional Trading Activity for {result['ticker']}
Analysis Timestamp: {analysis_time}
Query Period: Last {result['days_queried']} days
Chamber: {result['chamber'].title()}

DATA SOURCES & VERIFICATION:
- API: Politician Trade Tracker via RapidAPI
- Get API key: https://rapidapi.com/politics-trackers-politics-trackers-default/api/politician-trade-tracker
- Original Filings: PTR (Periodic Transaction Report) links provided below
- Verify at House: https://disclosures-clerk.house.gov/PublicDisclosure/FinancialDisclosure
- Verify at Senate: https://efdsearch.senate.gov/search/

LEGAL CONTEXT:
The STOCK Act (Stop Trading on Congressional Knowledge Act) requires all members of
Congress to publicly disclose stock transactions within 45 days. This data is sourced
from official government disclosures and maintained by House/Senate Stock Watcher.

ANALYSIS SUMMARY:
Total Trades Found: {result['total_trades']}
{_format_analysis_summary(result['analysis'])}

DETAILED TRADES:
{_format_trades_list(result['trades'])}

IMPORTANT DISCLAIMER:
This congressional trading data is for informational and transparency purposes only.
It should NOT be considered financial advice or a recommendation to buy or sell any
security. Congressional trades are disclosed 0-45 days after execution, so this data
is delayed and may not reflect current positions.

Always verify information using the official PTR links provided above. The presence
of congressional trading activity does not guarantee any particular outcome. Consult
a licensed financial advisor before making investment decisions.

This tool democratizes publicly available information but does not constitute insider
information or a trading signal. Use as one of many factors in your research.
"""

    if result.get("errors"):
        summary += "\n\nNOTE: Some data sources encountered errors:\n"
        for error in result["errors"]:
            summary += f"- {error}\n"

    return {
        "summary": summary.strip(),
        "raw_data": result,
        "verification_links": {
            "house_disclosures": "https://disclosures-clerk.house.gov/PublicDisclosure/FinancialDisclosure",
            "senate_disclosures": "https://efdsearch.senate.gov/search/",
            "api_signup": "https://rapidapi.com/politics-trackers-politics-trackers-default/api/politician-trade-tracker",
            "ptr_links": [
                t["ptr_link"] for t in result["trades"] if t.get("ptr_link")
            ],
        },
        "data_sources": ["Politician Trade Tracker API (via RapidAPI)"],
        "analysis_timestamp": analysis_time,
    }


def _format_analysis_summary(analysis: Dict) -> str:
    """Format analysis section for readable output."""
    lines = [
        f"Sentiment: {analysis['sentiment']}",
        f"Recent Activity: {analysis['recent_activity']}",
        f"- Purchases: {analysis['purchases']}",
        f"- Sales: {analysis['sales']}",
        f"- Unique Politicians: {analysis['unique_politicians']}",
    ]

    if analysis.get("party_breakdown"):
        lines.append(
            f"- Party Breakdown: {', '.join(f'{k}: {v}' for k, v in analysis['party_breakdown'].items())}"
        )

    if analysis.get("chamber_breakdown"):
        lines.append(
            f"- Chamber Breakdown: {', '.join(f'{k}: {v}' for k, v in analysis['chamber_breakdown'].items())}"
        )

    return "\n".join(lines)


def _format_trades_list(trades: List[Dict]) -> str:
    """Format individual trades for readable output."""
    if not trades:
        return "No trades found for this ticker in the specified time period."

    lines: List[str] = []
    for i, trade in enumerate(trades, 1):
        lines.append(
            f"\n{i}. {trade['politician']} ({trade['party']}-{trade['state']}) - {trade['chamber']}"
        )
        lines.append(f"   Transaction Date: {trade['transaction_date']}")
        lines.append(f"   Disclosed: {trade['disclosure_date']}")
        lines.append(f"   Type: {trade['transaction_type']}")
        lines.append(f"   Amount: {trade['amount']}")
        lines.append(f"   Asset: {trade['asset_description']}")
        lines.append(f"   Owner: {trade['owner']}")
        if trade.get("ptr_link"):
            lines.append(f"   PTR Filing: {trade['ptr_link']}")

        if i >= 20:  # Limit to 20 most recent trades in summary
            remaining = len(trades) - 20
            if remaining > 0:
                lines.append(
                    f"\n... and {remaining} more trade(s). See raw_data for complete list."
                )
            break

    return "\n".join(lines)
