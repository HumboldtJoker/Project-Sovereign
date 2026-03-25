"""
Aggregate congressional-trade trend analysis.

Transforms individual congressional trade disclosures into aggregate trend
analysis.  Addresses the 0-45 day disclosure delay limitation by identifying
patterns across multiple Congress members over time.

Key Insight: Individual delayed trades are noise. Patterns across 535 members
are signal.
"""

import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import requests
import yfinance as yf

from core.config import RAPIDAPI_KEY

logger = logging.getLogger(__name__)


def get_all_recent_trades(
    api_key: Optional[str] = None,
    limit: int = 100,
) -> Tuple[List[Dict], Optional[str]]:
    """
    Fetch the most recent congressional trades across all politicians.

    Args:
        api_key: RapidAPI key (falls back to config / env var)
        limit: Maximum trades to fetch (API returns 100 by default)

    Returns:
        Tuple of (trades_list, error_message)
    """
    if api_key is None:
        api_key = RAPIDAPI_KEY

    if not api_key:
        return ([], "API key required. Set RAPIDAPI_KEY environment variable.")

    try:
        url = "https://politician-trade-tracker1.p.rapidapi.com/get_latest_trades"
        headers = {
            "X-RapidAPI-Key": api_key,
            "X-RapidAPI-Host": "politician-trade-tracker1.p.rapidapi.com",
        }

        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()

        all_trades_data = response.json()

        # Parse trades
        trades_list = (
            all_trades_data
            if isinstance(all_trades_data, list)
            else all_trades_data.get("trades", [])
        )

        # Normalize trade data
        normalized_trades: List[Dict] = []
        for trade in trades_list[:limit]:
            # Parse date
            try:
                trans_date_str = trade.get("trade_date", "")
                trans_date = datetime.strptime(trans_date_str, "%B %d, %Y")
            except (ValueError, TypeError):
                continue

            # Extract ticker (format: "TICKER:US" -> "TICKER")
            ticker_raw = trade.get("ticker", "").upper().strip()
            if ":" in ticker_raw:
                ticker = ticker_raw.split(":")[0]
            else:
                ticker = ticker_raw

            # Skip N/A tickers
            if ticker == "N/A" or not ticker:
                continue

            normalized_trades.append(
                {
                    "ticker": ticker,
                    "politician": trade.get("name", "Unknown"),
                    "party": trade.get("party", "Unknown"),
                    "chamber": trade.get("chamber", "Unknown"),
                    "state": trade.get(
                        "state_abbreviation",
                        trade.get("state_name", "Unknown"),
                    ),
                    "transaction_date": trans_date.strftime("%Y-%m-%d"),
                    "transaction_type": trade.get("trade_type", "Unknown").lower(),
                    "amount": trade.get("trade_amount", "Unknown"),
                    "company": trade.get("company", "Unknown"),
                    "days_old": (datetime.now() - trans_date).days,
                }
            )

        return (normalized_trades, None)

    except Exception as e:
        logger.exception("Failed to fetch aggregate trades")
        return ([], f"Failed to fetch trades: {str(e)}")


def analyze_ticker_sentiment(trades: List[Dict]) -> Dict:
    """
    Analyze aggregate buy/sell sentiment by ticker.

    Returns:
        dict with ticker sentiment analysis
    """
    ticker_stats: Dict = defaultdict(
        lambda: {
            "buys": 0,
            "sells": 0,
            "buyers": set(),
            "sellers": set(),
            "total_politicians": set(),
            "party_breakdown": defaultdict(int),
            "recent_buys": 0,  # Last 30 days
            "recent_sells": 0,
        }
    )

    for trade in trades:
        ticker = trade["ticker"]
        politician = trade["politician"]
        party = trade["party"]
        tx_type = trade["transaction_type"]
        days_old = trade["days_old"]

        ticker_stats[ticker]["total_politicians"].add(politician)
        ticker_stats[ticker]["party_breakdown"][party] += 1

        if "buy" in tx_type or "purchase" in tx_type:
            ticker_stats[ticker]["buys"] += 1
            ticker_stats[ticker]["buyers"].add(politician)
            if days_old <= 30:
                ticker_stats[ticker]["recent_buys"] += 1
        elif "sell" in tx_type or "sale" in tx_type:
            ticker_stats[ticker]["sells"] += 1
            ticker_stats[ticker]["sellers"].add(politician)
            if days_old <= 30:
                ticker_stats[ticker]["recent_sells"] += 1

    # Calculate sentiment scores
    results: Dict = {}
    for ticker, stats in ticker_stats.items():
        net_sentiment = stats["buys"] - stats["sells"]
        total_trades = stats["buys"] + stats["sells"]
        sentiment_ratio = net_sentiment / total_trades if total_trades > 0 else 0

        # Determine sentiment label
        if sentiment_ratio > 0.5:
            sentiment = "STRONG BULLISH"
        elif sentiment_ratio > 0.2:
            sentiment = "BULLISH"
        elif sentiment_ratio > -0.2:
            sentiment = "NEUTRAL"
        elif sentiment_ratio > -0.5:
            sentiment = "BEARISH"
        else:
            sentiment = "STRONG BEARISH"

        results[ticker] = {
            "ticker": ticker,
            "total_trades": total_trades,
            "buys": stats["buys"],
            "sells": stats["sells"],
            "net_sentiment": net_sentiment,
            "sentiment_ratio": round(sentiment_ratio, 2),
            "sentiment": sentiment,
            "unique_politicians": len(stats["total_politicians"]),
            "unique_buyers": len(stats["buyers"]),
            "unique_sellers": len(stats["sellers"]),
            "party_breakdown": dict(stats["party_breakdown"]),
            "recent_activity": {
                "buys_30d": stats["recent_buys"],
                "sells_30d": stats["recent_sells"],
            },
        }

    # Sort by total trades (most active first)
    return dict(
        sorted(results.items(), key=lambda x: x[1]["total_trades"], reverse=True)
    )


def analyze_sector_trends(trades: List[Dict]) -> Dict:
    """
    Aggregate congressional trading by sector.

    Uses yfinance to map tickers to sectors, then analyzes sector-level
    sentiment.
    """
    # Map tickers to sectors
    ticker_sectors: Dict[str, str] = {}
    unique_tickers = set(t["ticker"] for t in trades)

    for ticker in unique_tickers:
        try:
            stock = yf.Ticker(ticker)
            info = stock.info
            if not info or "sector" not in info:
                ticker_sectors[ticker] = "Unknown"
            else:
                sector = info.get("sector", "Unknown")
                ticker_sectors[ticker] = sector if sector else "Unknown"
        except Exception:
            ticker_sectors[ticker] = "Unknown"

    # Aggregate by sector
    sector_stats: Dict = defaultdict(
        lambda: {
            "buys": 0,
            "sells": 0,
            "tickers": set(),
            "politicians": set(),
        }
    )

    for trade in trades:
        ticker = trade["ticker"]
        sector = ticker_sectors.get(ticker, "Unknown")
        tx_type = trade["transaction_type"]

        sector_stats[sector]["tickers"].add(ticker)
        sector_stats[sector]["politicians"].add(trade["politician"])

        if "buy" in tx_type or "purchase" in tx_type:
            sector_stats[sector]["buys"] += 1
        elif "sell" in tx_type or "sale" in tx_type:
            sector_stats[sector]["sells"] += 1

    # Calculate sector sentiment
    results: Dict = {}
    for sector, stats in sector_stats.items():
        if sector == "Unknown":
            continue

        total_trades = stats["buys"] + stats["sells"]
        net_sentiment = stats["buys"] - stats["sells"]

        results[sector] = {
            "sector": sector,
            "total_trades": total_trades,
            "buys": stats["buys"],
            "sells": stats["sells"],
            "net_sentiment": net_sentiment,
            "unique_tickers": len(stats["tickers"]),
            "unique_politicians": len(stats["politicians"]),
            "top_tickers": list(stats["tickers"])[:5],
        }

    return dict(
        sorted(results.items(), key=lambda x: x[1]["total_trades"], reverse=True)
    )


def analyze_party_divergence(trades: List[Dict]) -> List[Dict]:
    """
    Identify tickers where Democrats and Republicans are trading opposite
    directions.

    Interesting for detecting partisan information asymmetries.
    """
    ticker_party_stats: Dict = defaultdict(
        lambda: {
            "Democrat": {"buys": 0, "sells": 0},
            "Republican": {"buys": 0, "sells": 0},
        }
    )

    for trade in trades:
        ticker = trade["ticker"]
        party = trade["party"]
        tx_type = trade["transaction_type"]

        if party not in ["Democrat", "Republican"]:
            continue

        if "buy" in tx_type or "purchase" in tx_type:
            ticker_party_stats[ticker][party]["buys"] += 1
        elif "sell" in tx_type or "sale" in tx_type:
            ticker_party_stats[ticker][party]["sells"] += 1

    # Find divergences
    divergences: List[Dict] = []
    for ticker, party_data in ticker_party_stats.items():
        dem_net = party_data["Democrat"]["buys"] - party_data["Democrat"]["sells"]
        rep_net = party_data["Republican"]["buys"] - party_data["Republican"]["sells"]

        # Require at least 2 trades from each party
        dem_total = party_data["Democrat"]["buys"] + party_data["Democrat"]["sells"]
        rep_total = party_data["Republican"]["buys"] + party_data["Republican"]["sells"]

        if dem_total < 2 or rep_total < 2:
            continue

        # Check for opposite directions
        if (dem_net > 0 and rep_net < 0) or (dem_net < 0 and rep_net > 0):
            divergences.append(
                {
                    "ticker": ticker,
                    "democrat_sentiment": "BUYING" if dem_net > 0 else "SELLING",
                    "republican_sentiment": "BUYING" if rep_net > 0 else "SELLING",
                    "democrat_trades": (
                        f"{party_data['Democrat']['buys']} buys, "
                        f"{party_data['Democrat']['sells']} sells"
                    ),
                    "republican_trades": (
                        f"{party_data['Republican']['buys']} buys, "
                        f"{party_data['Republican']['sells']} sells"
                    ),
                    "divergence_strength": abs(dem_net - rep_net),
                }
            )

    return sorted(divergences, key=lambda x: x["divergence_strength"], reverse=True)


def get_aggregate_analysis(api_key: Optional[str] = None) -> Dict:
    """
    Main interface for aggregate congressional trading analysis.

    Returns comprehensive analysis of recent congressional trading patterns.
    """
    # Fetch all recent trades
    trades, error = get_all_recent_trades(api_key)

    if error:
        return {"error": error}

    if not trades:
        return {"error": "No trades found"}

    # Run all analyses
    ticker_sentiment = analyze_ticker_sentiment(trades)
    sector_trends = analyze_sector_trends(trades)
    party_divergence = analyze_party_divergence(trades)

    # Overall statistics
    total_trades = len(trades)
    unique_politicians = len(set(t["politician"] for t in trades))
    unique_tickers = len(set(t["ticker"] for t in trades))

    # Date range
    dates = [
        datetime.strptime(t["transaction_date"], "%Y-%m-%d") for t in trades
    ]
    oldest_trade = min(dates).strftime("%Y-%m-%d")
    newest_trade = max(dates).strftime("%Y-%m-%d")

    # Generate summary
    analysis_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC")

    summary = f"""
CONGRESSIONAL TRADING AGGREGATE ANALYSIS
Analysis Timestamp: {analysis_time}
Data Coverage: {newest_trade} to {oldest_trade}
Total Trades Analyzed: {total_trades}
Unique Politicians: {unique_politicians}
Unique Tickers: {unique_tickers}

METHODOLOGY:
This analysis aggregates individual congressional trades (delayed 0-45 days by STOCK Act)
into trend patterns. Individual delayed trades are noise; patterns across 535 members
are signal. Focus on stocks with multiple Congress members trading in same direction.

===================================================================
TOP TRENDING STOCKS (By Trading Volume)
===================================================================

{_format_ticker_sentiment(ticker_sentiment, top_n=15)}

===================================================================
SECTOR TRENDS
===================================================================

{_format_sector_trends(sector_trends)}

===================================================================
PARTISAN DIVERGENCES
===================================================================

{_format_party_divergence(party_divergence)}

INTERPRETATION GUIDE:
- High politician count = Widespread conviction (more reliable signal)
- Recent activity (30d) vs older = Accelerating or decelerating interest
- Party divergence = Potential partisan information asymmetry
- Sector accumulation = Thematic legislative positioning

IMPORTANT DISCLAIMER:
This aggregate analysis is for informational purposes only and NOT financial advice.
Congressional trades are disclosed 0-45 days after execution. This analysis identifies
patterns across multiple members but does not guarantee future performance.

Aggregate patterns may indicate where Congress sees legislative/regulatory opportunities,
but should be one of many factors in investment research. Consult a licensed financial
advisor before making investment decisions.
"""

    return {
        "summary": summary.strip(),
        "raw_data": {
            "ticker_sentiment": ticker_sentiment,
            "sector_trends": sector_trends,
            "party_divergence": party_divergence,
            "metadata": {
                "total_trades": total_trades,
                "unique_politicians": unique_politicians,
                "unique_tickers": unique_tickers,
                "date_range": f"{oldest_trade} to {newest_trade}",
            },
        },
        "analysis_timestamp": analysis_time,
    }


def _format_ticker_sentiment(ticker_sentiment: Dict, top_n: int = 15) -> str:
    """Format ticker sentiment analysis for display."""
    lines: List[str] = []
    lines.append(
        "Ticker  Trades  Buy/Sell  Net  Sentiment         Politicians  Recent(30d)"
    )
    lines.append("-" * 80)

    for _i, (ticker, data) in enumerate(
        list(ticker_sentiment.items())[:top_n], 1
    ):
        recent = (
            f"{data['recent_activity']['buys_30d']}B/"
            f"{data['recent_activity']['sells_30d']}S"
        )

        lines.append(
            f"{ticker:6s}  {data['total_trades']:5d}   "
            f"{data['buys']:3d}/{data['sells']:3d}   {data['net_sentiment']:+4d}  "
            f"{data['sentiment']:17s}  {data['unique_politicians']:3d} pols    {recent}"
        )

    return "\n".join(lines)


def _format_sector_trends(sector_trends: Dict) -> str:
    """Format sector trends for display."""
    lines: List[str] = []
    lines.append(
        "Sector                      Trades  Buy/Sell  Net   Politicians  Top Tickers"
    )
    lines.append("-" * 90)

    for sector, data in sector_trends.items():
        top_tickers = ", ".join(data["top_tickers"][:3])
        lines.append(
            f"{sector:26s}  {data['total_trades']:5d}   "
            f"{data['buys']:3d}/{data['sells']:3d}   {data['net_sentiment']:+4d}  "
            f"{data['unique_politicians']:3d} pols     {top_tickers}"
        )

    return "\n".join(lines)


def _format_party_divergence(divergences: List[Dict]) -> str:
    """Format party divergence analysis."""
    if not divergences:
        return "No significant partisan divergences detected in recent trades."

    lines: List[str] = []
    lines.append(
        "Stocks where Democrats and Republicans are trading opposite directions:\n"
    )

    for div in divergences[:10]:
        lines.append(f"  {div['ticker']}:")
        lines.append(
            f"    Democrats: {div['democrat_sentiment']} ({div['democrat_trades']})"
        )
        lines.append(
            f"    Republicans: {div['republican_sentiment']} ({div['republican_trades']})"
        )
        lines.append(f"    Divergence Strength: {div['divergence_strength']}")
        lines.append("")

    return "\n".join(lines)
