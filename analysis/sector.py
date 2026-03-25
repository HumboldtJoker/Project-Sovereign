"""
Sector allocation and concentration-risk analysis.

Analyzes portfolio diversification across market sectors to identify
concentration risks.  Compares portfolio sector exposure to S&P 500
benchmark weights.

Key Metrics:
- Sector Exposure: Percentage of portfolio in each sector
- Concentration Risk: Sectors with >30% allocation
- Diversification Score: How well-distributed across sectors
- Benchmark Comparison: Vs. S&P 500 sector weights
"""

import logging
import math
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Optional

import pandas as pd  # noqa: F401
import yfinance as yf

from core.config import SP500_SECTOR_WEIGHTS

logger = logging.getLogger(__name__)


def analyze_sector_allocation(
    tickers: Optional[List[str]] = None,
    weights: Optional[List[float]] = None,
) -> Dict:
    """
    Main interface for sector allocation analysis.

    Args:
        tickers: List of stock symbols (e.g., ['AAPL', 'MSFT', 'XOM'])
        weights: Optional list of portfolio weights (must sum to 1.0).
                 If None, assumes equal weighting.

    Returns:
        Dict with formatted summary and raw data
    """
    if tickers is None:
        tickers = []

    result = get_sector_allocation(tickers, weights)

    if "error" in result:
        return result

    # Generate timestamp
    analysis_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC")

    # Format summary
    summary = f"""
Sector Allocation Analysis
Analysis Timestamp: {analysis_time}
Portfolio Size: {len(tickers)} stocks
Total Portfolio Value: ${result['total_value']:,.2f} (normalized)

DATA SOURCES & VERIFICATION:
- Sector Data: Yahoo Finance (via yfinance Python library)
- Verify at: https://finance.yahoo.com/
- S&P 500 Benchmark: SPDR Sector ETF holdings
- Verify at: https://www.ssga.com/us/en/institutional/etfs/spdr-sector-etfs

METHODOLOGY:
- Sector exposure calculated as weighted percentage of portfolio
- Concentration risk flagged when single sector exceeds 30%
- Diversification score based on distribution across sectors (0-100)
- Benchmark comparison shows over/under-weight vs. S&P 500

===================================================================
PORTFOLIO SECTOR BREAKDOWN
===================================================================

{_format_sector_exposure(result['sector_exposure'], result['total_value'])}

===================================================================
CONCENTRATION RISK ANALYSIS
===================================================================

{_format_concentration_risk(result['sector_exposure'])}

===================================================================
BENCHMARK COMPARISON (vs. S&P 500)
===================================================================

{_format_benchmark_comparison(result['sector_exposure'])}

===================================================================
INDIVIDUAL HOLDINGS BY SECTOR
===================================================================

{_format_holdings_by_sector(result['holdings'])}

===================================================================
DIVERSIFICATION ASSESSMENT
===================================================================

{_format_diversification_assessment(result['sector_exposure'], len(tickers))}

IMPORTANT DISCLAIMER:
This sector allocation analysis is for informational purposes only and should NOT
be considered financial advice. Sector concentration analysis is based on current
holdings and may change with market movements. A well-diversified portfolio
typically limits single-sector exposure to 20-30% maximum.

High concentration in one sector exposes the portfolio to sector-specific risks.
During sector downturns (e.g., 2000 tech crash, 2008 financial crisis), concentrated
portfolios can experience amplified losses.

Always verify sector classifications independently using the links provided above.
Consult a licensed financial advisor before making investment decisions.

Data calculated from Yahoo Finance sector classifications and S&P 500 benchmark
weights. All metrics are point-in-time and subject to change.
"""

    return {
        "summary": summary.strip(),
        "raw_data": result,
        "verification_links": {
            "yahoo_finance": "https://finance.yahoo.com/",
            "spdr_sectors": "https://www.ssga.com/us/en/institutional/etfs/spdr-sector-etfs",
            "portfolio_tickers": [
                f"https://finance.yahoo.com/quote/{t}" for t in tickers
            ],
        },
        "data_source": "Yahoo Finance",
        "analysis_timestamp": analysis_time,
    }


def get_sector_allocation(
    tickers: Optional[List[str]] = None,
    weights: Optional[List[float]] = None,
) -> Dict:
    """
    Calculate sector allocation for a portfolio.

    Args:
        tickers: List of stock symbols
        weights: Optional portfolio weights (must sum to 1.0)

    Returns:
        Dict containing sector exposure, holdings details, and metrics
    """
    if tickers is None:
        tickers = []

    try:
        # Validate inputs
        if not tickers or len(tickers) == 0:
            return {"error": "Need at least 1 ticker for sector analysis"}

        # Clean tickers
        tickers = [t.upper().strip() for t in tickers]

        # Validate weights
        if weights is None:
            # Equal weighting
            weights = [1.0 / len(tickers)] * len(tickers)
        else:
            if len(weights) != len(tickers):
                return {"error": "Number of weights must match number of tickers"}
            if abs(sum(weights) - 1.0) > 0.01:
                return {
                    "error": f"Weights must sum to 1.0 (got {sum(weights):.4f})"
                }

        # Fetch sector data for each ticker
        holdings: List[Dict] = []
        sector_exposure: Dict[str, float] = defaultdict(float)
        total_value: float = 1000000.0  # Normalized to $1M for display

        failed_tickers: List[tuple] = []

        for ticker, weight in zip(tickers, weights):
            try:
                stock = yf.Ticker(ticker)
                info = stock.info

                # Get sector (may be None for some tickers)
                sector = info.get("sector", "Unknown")
                if not sector or sector == "None":
                    sector = "Unknown"

                # Get current price
                current_price = info.get("currentPrice") or info.get(
                    "regularMarketPrice"
                )
                if not current_price:
                    # Try to get from history
                    hist = stock.history(period="1d")
                    if not hist.empty:
                        current_price = hist["Close"].iloc[-1]
                    else:
                        current_price = 100.0  # Fallback for display
                        logger.warning(
                            "No price data available for %s, using fallback price $%.2f",
                            ticker,
                            current_price,
                        )

                # Calculate position value
                position_value = total_value * weight

                holdings.append(
                    {
                        "ticker": ticker,
                        "sector": sector,
                        "weight": weight,
                        "position_value": position_value,
                        "current_price": current_price,
                        "company_name": info.get("longName", ticker),
                    }
                )

                # Accumulate sector exposure
                sector_exposure[sector] += weight

            except Exception as e:
                failed_tickers.append((ticker, str(e)))
                # Add as Unknown sector with weight
                holdings.append(
                    {
                        "ticker": ticker,
                        "sector": "Unknown",
                        "weight": weight,
                        "position_value": total_value * weight,
                        "current_price": 0.0,
                        "company_name": ticker,
                    }
                )
                sector_exposure["Unknown"] += weight

        # Convert sector exposure to percentages
        sector_exposure_pct: Dict[str, float] = {
            sector: exposure * 100
            for sector, exposure in sector_exposure.items()
        }

        # Sort by exposure (descending)
        sector_exposure_pct = dict(
            sorted(sector_exposure_pct.items(), key=lambda x: x[1], reverse=True)
        )

        # Calculate diversification score
        # Based on entropy - higher entropy = better diversification
        num_sectors = len(
            [s for s in sector_exposure_pct.keys() if s != "Unknown"]
        )
        if num_sectors > 1:
            # Calculate normalized entropy
            entropy: float = 0
            for exposure in sector_exposure.values():
                if exposure > 0:
                    entropy -= exposure * math.log(exposure)
            max_entropy = math.log(num_sectors)
            diversification_score = (
                int((entropy / max_entropy) * 100) if max_entropy > 0 else 0
            )
        else:
            diversification_score = 0

        return {
            "tickers": tickers,
            "weights": weights,
            "holdings": holdings,
            "sector_exposure": sector_exposure_pct,
            "total_value": total_value,
            "num_sectors": num_sectors,
            "diversification_score": diversification_score,
            "failed_tickers": failed_tickers,
        }

    except Exception as e:
        logger.exception("Failed to calculate sector allocation")
        return {"error": f"Failed to calculate sector allocation: {str(e)}"}


def _format_sector_exposure(
    sector_exposure: Dict[str, float], total_value: float
) -> str:
    """Format sector exposure section."""
    lines: List[str] = []
    lines.append(
        "Sector                      Exposure    Value        Assessment"
    )
    lines.append("-" * 70)

    for sector, pct in sector_exposure.items():
        if sector == "Unknown":
            continue

        value = total_value * (pct / 100)

        # Assessment
        if pct > 30:
            assessment = "[HIGH RISK] Concentrated"
        elif pct > 20:
            assessment = "[CAUTION] Above-target"
        elif pct > 10:
            assessment = "[OK] Moderate"
        else:
            assessment = "[OK] Minor position"

        lines.append(
            f"{sector:26s} {pct:6.1f}%    ${value:>11,.0f}  {assessment}"
        )

    # Add Unknown if present
    if "Unknown" in sector_exposure and sector_exposure["Unknown"] > 0:
        pct = sector_exposure["Unknown"]
        value = total_value * (pct / 100)
        lines.append(
            f"{'Unknown':26s} {pct:6.1f}%    ${value:>11,.0f}  [WARN] Sector unknown"
        )

    return "\n".join(lines)


def _format_concentration_risk(sector_exposure: Dict[str, float]) -> str:
    """Format concentration risk analysis."""
    high_concentration = {
        sector: pct
        for sector, pct in sector_exposure.items()
        if pct > 30 and sector != "Unknown"
    }

    moderate_concentration = {
        sector: pct
        for sector, pct in sector_exposure.items()
        if 20 < pct <= 30 and sector != "Unknown"
    }

    lines: List[str] = []

    if high_concentration:
        lines.append("[HIGH RISK] Sectors with >30% exposure:")
        lines.append("")
        for sector, pct in high_concentration.items():
            lines.append(f"  {sector}: {pct:.1f}%")
            lines.append("    -> This sector dominates your portfolio")
            lines.append(
                "    -> Sector-specific downturns will heavily impact returns"
            )
        lines.append("")

    if moderate_concentration:
        lines.append("[CAUTION] Sectors with 20-30% exposure:")
        lines.append("")
        for sector, pct in moderate_concentration.items():
            lines.append(f"  {sector}: {pct:.1f}%")
            lines.append("    -> Consider reducing exposure below 20%")
        lines.append("")

    if not high_concentration and not moderate_concentration:
        lines.append("[OK] No significant concentration risks detected")
        lines.append("  All sectors below 20% exposure threshold")

    return "\n".join(lines)


def _format_benchmark_comparison(sector_exposure: Dict[str, float]) -> str:
    """Format benchmark comparison."""
    lines: List[str] = []
    lines.append(
        "Sector                      Portfolio    S&P 500    Difference"
    )
    lines.append("-" * 70)

    # Get all sectors (union of portfolio and benchmark)
    all_sectors = set(sector_exposure.keys()) | set(SP500_SECTOR_WEIGHTS.keys())
    all_sectors.discard("Unknown")

    # Sort by portfolio weight
    sorted_sectors = sorted(
        all_sectors,
        key=lambda s: sector_exposure.get(s, 0),
        reverse=True,
    )

    for sector in sorted_sectors:
        port_pct = sector_exposure.get(sector, 0.0)
        bench_pct = SP500_SECTOR_WEIGHTS.get(sector, 0.0)
        diff = port_pct - bench_pct

        # Format difference
        if abs(diff) < 5:
            status = "Aligned"
        elif diff > 10:
            status = "OVERWEIGHT"
        elif diff < -10:
            status = "Underweight"
        elif diff > 5:
            status = "Overweight"
        else:
            status = "Underweight"

        diff_str = f"{diff:+.1f}%"
        lines.append(
            f"{sector:26s} {port_pct:6.1f}%    {bench_pct:6.1f}%    "
            f"{diff_str:>8s}  {status}"
        )

    return "\n".join(lines)


def _format_holdings_by_sector(holdings: List[Dict]) -> str:
    """Format individual holdings grouped by sector."""
    # Group by sector
    by_sector: Dict[str, List[Dict]] = defaultdict(list)
    for holding in holdings:
        by_sector[holding["sector"]].append(holding)

    # Sort sectors by total weight
    sector_weights = {
        sector: sum(h["weight"] for h in holds)
        for sector, holds in by_sector.items()
    }
    sorted_sectors = sorted(
        sector_weights.items(), key=lambda x: x[1], reverse=True
    )

    lines: List[str] = []
    for sector, _ in sorted_sectors:
        if sector == "Unknown":
            continue

        lines.append(f"\n{sector}:")

        # Sort holdings within sector by weight
        sector_holdings = sorted(
            by_sector[sector],
            key=lambda h: h["weight"],
            reverse=True,
        )

        for h in sector_holdings:
            weight_pct = h["weight"] * 100
            lines.append(
                f"  {h['ticker']:6s} ({h['company_name'][:30]:30s}) "
                f"{weight_pct:5.1f}%  ${h['position_value']:>11,.0f}"
            )

    # Add Unknown if present
    if "Unknown" in by_sector:
        lines.append("\nUnknown Sector:")
        for h in by_sector["Unknown"]:
            weight_pct = h["weight"] * 100
            lines.append(
                f"  {h['ticker']:6s} {weight_pct:5.1f}%  [Sector data unavailable]"
            )

    return "\n".join(lines)


def _format_diversification_assessment(
    sector_exposure: Dict[str, float],
    num_tickers: int,
) -> str:
    """Format diversification assessment."""
    num_sectors = len(
        [s for s in sector_exposure.keys() if s != "Unknown"]
    )
    max_exposure = max(sector_exposure.values())

    lines: List[str] = []
    lines.append("Portfolio Statistics:")
    lines.append(f"  Number of holdings: {num_tickers}")
    lines.append(f"  Number of sectors: {num_sectors}")
    lines.append(f"  Largest sector exposure: {max_exposure:.1f}%")
    lines.append("")

    # Overall assessment
    if max_exposure > 40:
        level = "POOR"
        color = "[RED]"
        explanation = (
            "Your portfolio is heavily concentrated in one sector. This creates\n"
            "significant risk if that sector underperforms. Consider reducing\n"
            "exposure to below 30% through rebalancing."
        )
    elif max_exposure > 30:
        level = "FAIR"
        color = "[ORANGE]"
        explanation = (
            "Your portfolio has notable concentration in one sector. While not\n"
            "critical, consider diversifying to reduce sector-specific risk."
        )
    elif num_sectors < 4:
        level = "FAIR"
        color = "[ORANGE]"
        explanation = (
            "Your portfolio is spread across only a few sectors. Consider adding\n"
            "exposure to additional sectors for better diversification."
        )
    elif max_exposure > 25:
        level = "GOOD"
        color = "[YELLOW]"
        explanation = (
            "Your portfolio shows reasonable sector diversification with moderate\n"
            "concentration. Overall balance is acceptable."
        )
    else:
        level = "EXCELLENT"
        color = "[GREEN]"
        explanation = (
            "Your portfolio shows strong sector diversification with no significant\n"
            "concentration risks. Good balance across multiple sectors."
        )

    lines.append(f"Diversification Level: {level} {color}")
    lines.append("")
    lines.append(f"Assessment: {explanation}")

    return "\n".join(lines)
