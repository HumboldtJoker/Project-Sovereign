"""
Portfolio correlation and diversification analysis.

Analyzes portfolio diversification through correlation analysis and
volatility metrics.  Helps users understand if their holdings are truly
diverse or just different flavors of the same risk.

Key Metrics:
- Correlation Matrix: How closely stocks move together
- Standard Deviation: Price volatility ("spikiness")
- Beta: Movement relative to market
- Sharpe Ratio: Risk-adjusted returns
"""

import logging
from datetime import datetime
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)


def analyze_portfolio_correlation(
    tickers: Optional[List[str]] = None, period: str = "1y"
) -> Dict:
    """
    Main interface for portfolio correlation analysis.

    Args:
        tickers: List of stock symbols (e.g., ['AAPL', 'MSFT', 'GOOGL']).
                 Must contain at least 2 tickers.
        period: Historical period for analysis ('6mo', '1y', '2y', '5y')

    Returns:
        Dict with formatted summary and raw data
    """
    if tickers is None:
        tickers = []

    result = get_portfolio_metrics(tickers, period)

    if "error" in result:
        return result

    # Generate timestamp
    analysis_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC")

    # Format summary
    summary = f"""
Portfolio Diversification Analysis
Analysis Timestamp: {analysis_time}
Portfolio Size: {len(tickers)} stocks
Analysis Period: {period}
Market Benchmark: S&P 500 (^GSPC)

DATA SOURCES & VERIFICATION:
- Price Data: Yahoo Finance (via yfinance Python library)
- Verify at: https://finance.yahoo.com/
- Market Data (S&P 500): https://finance.yahoo.com/quote/%5EGSPC

METHODOLOGY:
- Correlation: Pearson correlation of daily returns (ranges -1 to +1)
  -> +1 = perfectly synchronized, 0 = independent, -1 = opposite moves
- Standard Deviation: Annualized volatility of returns (higher = more volatile)
- Beta: Sensitivity to market movements (1.0 = moves with market, >1 = amplified)
- Sharpe Ratio: Risk-adjusted returns (higher = better return per unit of risk)

===================================================================
PORTFOLIO OVERVIEW
===================================================================

{_format_portfolio_overview(result['stocks'])}

===================================================================
DIVERSIFICATION ANALYSIS
===================================================================

{_format_diversification_assessment(result['correlations'], result['stocks'])}

===================================================================
VOLATILITY METRICS (Individual Stocks)
===================================================================

{_format_volatility_metrics(result['stocks'])}

===================================================================
CORRELATION MATRIX
===================================================================

{_format_correlation_matrix(result['correlations'])}

===================================================================
RISK CLUSTERS (Highly Correlated Pairs)
===================================================================

{_format_risk_clusters(result['correlations'], threshold=0.7)}

IMPORTANT DISCLAIMER:
This portfolio analysis is for informational purposes only and should NOT be
considered financial advice. Correlation and volatility metrics are calculated
from historical data and may not predict future relationships. Past performance
does not guarantee future results.

A well-diversified portfolio should have low average correlation (<0.5) between
holdings. High correlation (>0.7) suggests concentrated risk - stocks may move
together during market stress, reducing diversification benefits.

Always verify the source data independently using the links provided above.
Consult a licensed financial advisor before making investment decisions.

Data calculated using standard industry formulas over the specified period.
All metrics are backward-looking and subject to change.
"""

    return {
        "summary": summary.strip(),
        "raw_data": result,
        "verification_links": {
            "yahoo_finance": "https://finance.yahoo.com/",
            "sp500": "https://finance.yahoo.com/quote/%5EGSPC",
            "portfolio_tickers": [
                f"https://finance.yahoo.com/quote/{t}" for t in tickers
            ],
        },
        "data_source": "Yahoo Finance",
        "analysis_timestamp": analysis_time,
    }


def get_portfolio_metrics(
    tickers: Optional[List[str]] = None, period: str = "1y"
) -> Dict:
    """
    Calculate comprehensive portfolio metrics.

    Args:
        tickers: List of stock symbols
        period: Historical period for analysis

    Returns:
        Dict containing correlation matrix, volatility metrics, and
        diversification scores
    """
    if tickers is None:
        tickers = []

    try:
        # Validate tickers
        if not tickers or len(tickers) < 2:
            return {"error": "Need at least 2 tickers for correlation analysis"}

        # Clean tickers
        tickers = [t.upper().strip() for t in tickers]

        # Download historical data
        data = yf.download(tickers, period=period, progress=False)

        if data.empty:
            return {"error": "No data available for the specified tickers"}

        # Get closing prices
        if len(tickers) == 1:
            closes = data["Close"].to_frame()
            closes.columns = [tickers[0]]
        else:
            closes = data["Close"]

        # Remove any tickers with insufficient data
        closes = closes.dropna(axis=1, how="all")
        valid_tickers: List[str] = closes.columns.tolist()

        if len(valid_tickers) < 2:
            return {"error": "Insufficient data for correlation analysis"}

        # Calculate daily returns
        returns = closes.pct_change().dropna()

        # Download S&P 500 for beta calculation
        spy_data = yf.download("^GSPC", period=period, progress=False)
        market_returns = spy_data["Close"].pct_change().dropna()

        # Calculate correlation matrix
        correlation_matrix = returns.corr()

        # Calculate metrics for each stock
        stock_metrics: Dict[str, Dict] = {}
        for ticker in valid_tickers:
            stock_returns = returns[ticker]

            # Standard deviation (annualized)
            std_dev: float = stock_returns.std() * np.sqrt(252)  # 252 trading days

            # Beta (vs S&P 500)
            # Require minimum 30 trading days for reliable beta calculation
            MIN_BETA_DATA_POINTS = 30
            aligned_returns = pd.concat(
                [stock_returns, market_returns], axis=1, join="inner"
            )
            if len(aligned_returns) >= MIN_BETA_DATA_POINTS:
                covariance = aligned_returns.cov().iloc[0, 1]
                market_variance = aligned_returns.iloc[:, 1].var()
                beta: Optional[float] = (
                    covariance / market_variance if market_variance != 0 else None
                )
            else:
                beta = None  # Insufficient data for reliable beta

            # Sharpe Ratio (assuming 0% risk-free rate for simplicity)
            mean_return: float = stock_returns.mean() * 252  # Annualized
            sharpe: Optional[float] = (
                mean_return / std_dev if std_dev != 0 else None
            )

            # Current price
            current_price: float = closes[ticker].iloc[-1]

            stock_metrics[ticker] = {
                "ticker": ticker,
                "current_price": round(current_price, 2),
                "std_dev": round(std_dev, 4),
                "beta": round(beta, 2) if beta is not None else None,
                "sharpe_ratio": round(sharpe, 2) if sharpe is not None else None,
                "annualized_return": round(mean_return * 100, 2),  # as percentage
            }

        # Calculate average correlation
        corr_values: List[float] = []
        for i in range(len(valid_tickers)):
            for j in range(i + 1, len(valid_tickers)):
                corr_values.append(correlation_matrix.iloc[i, j])

        avg_correlation: float = np.mean(corr_values) if corr_values else 0

        # Diversification score (0-100, higher is better)
        # Based on average correlation: 0 correlation = 100 score, 1 correlation = 0 score
        diversification_score: int = int((1 - avg_correlation) * 100)

        return {
            "tickers": valid_tickers,
            "period": period,
            "data_points": len(returns),
            "correlations": correlation_matrix.to_dict(),
            "stocks": stock_metrics,
            "avg_correlation": round(avg_correlation, 3),
            "diversification_score": diversification_score,
        }

    except Exception as e:
        logger.exception("Failed to calculate portfolio metrics")
        return {"error": f"Failed to calculate portfolio metrics: {str(e)}"}


def _format_portfolio_overview(stocks: Dict) -> str:
    """Format portfolio overview section."""
    lines: List[str] = []
    lines.append(
        "Stock          Price      Volatility  Beta   Sharpe  Ann. Return"
    )
    lines.append("\u2500" * 70)

    for ticker, metrics in stocks.items():
        price = f"${metrics['current_price']}"
        volatility = (
            f"{metrics['std_dev'] * 100:.1f}%" if metrics["std_dev"] else "N/A"
        )
        beta = (
            f"{metrics['beta']:.2f}" if metrics["beta"] is not None else "N/A"
        )
        sharpe = (
            f"{metrics['sharpe_ratio']:.2f}"
            if metrics["sharpe_ratio"] is not None
            else "N/A"
        )
        ann_return = (
            f"{metrics['annualized_return']:+.1f}%"
            if metrics["annualized_return"]
            else "N/A"
        )

        lines.append(
            f"{ticker:12s} {price:10s} {volatility:11s} {beta:6s} {sharpe:7s} {ann_return:>10s}"
        )

    return "\n".join(lines)


def _format_diversification_assessment(
    correlations: Dict, stocks: Dict
) -> str:
    """Format diversification assessment."""
    # Calculate average correlation
    corr_values: List[float] = []
    tickers = list(stocks.keys())

    for i, ticker1 in enumerate(tickers):
        for ticker2 in tickers[i + 1 :]:
            if ticker1 in correlations and ticker2 in correlations[ticker1]:
                corr_values.append(correlations[ticker1][ticker2])

    avg_corr: float = np.mean(corr_values) if corr_values else 0

    # Determine diversification level
    if avg_corr < 0.3:
        level = "EXCELLENT"
        tag = "[GREEN]"
        explanation = (
            "Your portfolio shows low correlation between holdings. This is ideal for\n"
            "reducing concentrated risk."
        )
    elif avg_corr < 0.5:
        level = "GOOD"
        tag = "[YELLOW]"
        explanation = (
            "Your portfolio has moderate correlation. There's some overlap in risk, but\n"
            "overall diversification is reasonable."
        )
    elif avg_corr < 0.7:
        level = "FAIR"
        tag = "[ORANGE]"
        explanation = (
            "Your portfolio shows notable correlation between holdings. Consider adding\n"
            "uncorrelated assets to improve diversification."
        )
    else:
        level = "POOR"
        tag = "[RED]"
        explanation = (
            "Your portfolio is highly correlated. Holdings tend to move together, which\n"
            "reduces diversification benefits during market stress."
        )

    lines: List[str] = []
    lines.append(f"Diversification Level: {level} {tag}")
    lines.append(f"Average Correlation: {avg_corr:.3f}")
    lines.append(f"Portfolio Size: {len(tickers)} stocks")
    lines.append("")
    lines.append(f"Assessment: {explanation}")

    return "\n".join(lines)


def _format_volatility_metrics(stocks: Dict) -> str:
    """Format volatility metrics with explanations."""
    lines: List[str] = []

    for ticker, metrics in stocks.items():
        lines.append(f"\n{ticker}:")
        lines.append(f"  Standard Deviation: {metrics['std_dev'] * 100:.1f}%")
        lines.append(
            "    -> The 'spikiness factor' - how much this stock bounces around"
        )

        if metrics["beta"] is not None:
            lines.append(f"  Beta: {metrics['beta']:.2f}")
            if metrics["beta"] > 1.5:
                lines.append(
                    "    -> HIGH: Amplifies market moves significantly (wild rides!)"
                )
            elif metrics["beta"] > 1.0:
                lines.append(
                    "    -> Moderate: Moves more than the market"
                )
            elif metrics["beta"] > 0.5:
                lines.append(
                    "    -> Low: Moves less than the market (more stable)"
                )
            else:
                lines.append(
                    "    -> Very Low: Minimal correlation to market movements"
                )

        if metrics["sharpe_ratio"] is not None:
            lines.append(f"  Sharpe Ratio: {metrics['sharpe_ratio']:.2f}")
            if metrics["sharpe_ratio"] > 1.0:
                lines.append(
                    "    -> GOOD: Decent return for the risk taken"
                )
            elif metrics["sharpe_ratio"] > 0:
                lines.append(
                    "    -> Fair: Positive return but consider the volatility"
                )
            else:
                lines.append(
                    "    -> Poor: Negative or minimal risk-adjusted return"
                )

    return "\n".join(lines)


def _format_correlation_matrix(correlations: Dict) -> str:
    """Format correlation matrix in readable form."""
    tickers = list(correlations.keys())

    lines: List[str] = []

    # Header
    header = "        " + "  ".join(f"{t:>6s}" for t in tickers)
    lines.append(header)
    lines.append("\u2500" * len(header))

    # Rows
    for ticker1 in tickers:
        row = f"{ticker1:6s}  "
        for ticker2 in tickers:
            if ticker1 == ticker2:
                row += "  1.00  "
            else:
                corr = correlations[ticker1].get(ticker2, 0)
                row += f"{corr:6.2f}  "
        lines.append(row)

    lines.append("")
    lines.append("Reading the matrix:")
    lines.append(
        "  1.00 = Perfect positive correlation (move in lockstep)"
    )
    lines.append("  0.00 = No correlation (independent movements)")
    lines.append(
        " -1.00 = Perfect negative correlation (move in opposite directions)"
    )
    lines.append("  >0.70 = High correlation (similar risk exposure)")

    return "\n".join(lines)


def _format_risk_clusters(
    correlations: Dict, threshold: float = 0.7
) -> str:
    """Identify and format highly correlated pairs (risk clusters)."""
    tickers = list(correlations.keys())
    high_corr_pairs: List[tuple] = []

    for i, ticker1 in enumerate(tickers):
        for ticker2 in tickers[i + 1 :]:
            if ticker1 in correlations and ticker2 in correlations[ticker1]:
                corr = correlations[ticker1][ticker2]
                if abs(corr) >= threshold:
                    high_corr_pairs.append((ticker1, ticker2, corr))

    if not high_corr_pairs:
        return (
            "[OK] No high correlation pairs found (>0.70)\n"
            "  Your portfolio shows good diversification at the pair level."
        )

    lines: List[str] = []
    lines.append(f"[WARNING] Found {len(high_corr_pairs)} highly correlated pair(s):")
    lines.append("")

    # Sort by absolute correlation
    high_corr_pairs.sort(key=lambda x: abs(x[2]), reverse=True)

    for ticker1, ticker2, corr in high_corr_pairs:
        lines.append(f"  {ticker1} <-> {ticker2}: {corr:.3f}")
        if corr > 0:
            lines.append(
                "    -> These stocks tend to move together (similar risk)"
            )
        else:
            lines.append(
                "    -> These stocks tend to move opposite (natural hedge)"
            )

    lines.append("")
    lines.append(
        "Consider: High positive correlation means these stocks may not provide"
    )
    lines.append(
        "true diversification. During market stress, they could decline together."
    )

    return "\n".join(lines)
