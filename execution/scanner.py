"""
AI Ecosystem Opportunity Discovery Scanner for PLGenesis Market Agent.

Scans for STRONG BUY signals across AI infrastructure and software stocks.
"""

import json
import logging
from typing import Dict, List, Optional

from analysis.technical import get_technical_indicators

logger = logging.getLogger(__name__)

# Define scan universe
SCAN_UNIVERSE = {
    'semiconductor': ['AVGO', 'MRVL', 'QCOM', 'INTC', 'SMCI', 'ARM', 'SNPS', 'CDNS', 'ASML', 'LRCX', 'AMAT', 'KLAC'],
    'hardware': ['DELL', 'HPE'],
    'ai_software': ['PLTR', 'SNOW', 'AI', 'PATH', 'UPST', 'S'],
    'cybersecurity': ['CRWD', 'PANW', 'ZS'],
    'cloud': ['AMZN', 'ORCL', 'CRM', 'NOW'],
}

# Current holdings (skip these in detailed analysis)
CURRENT_HOLDINGS = ['CDNS', 'META', 'MSFT', 'MU', 'NVDA', 'TSM']


def scan_category(category_name: str, tickers: List[str],
                  holdings: Optional[List[str]] = None) -> List[Dict]:
    """
    Scan a category of stocks for opportunities.

    Args:
        category_name: Name of the sector/category being scanned
        tickers: List of ticker symbols to scan
        holdings: List of currently held tickers to skip (defaults to CURRENT_HOLDINGS)

    Returns:
        List of opportunity dicts for stocks that meet STRONG BUY criteria
    """
    if holdings is None:
        holdings = CURRENT_HOLDINGS

    logger.info("Scanning category: %s (%d tickers)", category_name, len(tickers))

    opportunities = []

    for ticker in tickers:
        if ticker in holdings:
            logger.debug("%s: skipped (already owned)", ticker)
            continue

        try:
            tech = get_technical_indicators(ticker)

            if 'error' in tech:
                logger.warning("%s: error fetching technicals: %s", ticker, tech['error'])
                continue

            signal = tech.get('signal', 'N/A')
            rsi = tech.get('rsi', 0)
            bullish_pct = tech.get('bullish_pct', 0)
            price = tech.get('price', 0)
            macd_signal = tech.get('macd_signal', 'neutral')
            confidence = tech.get('confidence', 'N/A')

            logger.info(
                "%s | %s | RSI: %.1f | Bullish: %.0f%% | Price: $%.2f | MACD: %s | Conf: %s",
                ticker, signal, rsi, bullish_pct, price, macd_signal, confidence,
            )

            # Identify STRONG BUY candidates
            is_strong_buy = (
                signal in ['BUY', 'STRONG BUY'] and
                bullish_pct >= 60 and
                rsi < 70  # Not overbought
            )

            if is_strong_buy:
                opportunities.append({
                    'ticker': ticker,
                    'category': category_name,
                    'signal': signal,
                    'bullish_pct': bullish_pct,
                    'rsi': rsi,
                    'price': price,
                    'macd_signal': macd_signal,
                    'confidence': confidence,
                    'details': tech.get('details', ''),
                })

        except Exception as e:
            logger.error("%s: scan error: %s", ticker, e)

    return opportunities


def run_full_scan(holdings: Optional[List[str]] = None,
                  output_path: Optional[str] = None) -> List[Dict]:
    """
    Run a full ecosystem scan across all categories.

    Args:
        holdings: List of currently held tickers to skip (defaults to CURRENT_HOLDINGS)
        output_path: Optional path to write JSON results

    Returns:
        List of all opportunity dicts, sorted by bullish conviction (descending)
    """
    if holdings is None:
        holdings = CURRENT_HOLDINGS

    logger.info("Starting AI ecosystem opportunity scan")
    logger.info("Current holdings: %s", ', '.join(holdings))

    all_opportunities = []

    # Scan each category
    for category, tickers in SCAN_UNIVERSE.items():
        opps = scan_category(category, tickers, holdings=holdings)
        all_opportunities.extend(opps)

    # Sort by conviction (bullish_pct)
    all_opportunities.sort(key=lambda x: x['bullish_pct'], reverse=True)

    logger.info("Scan complete: %d opportunities found", len(all_opportunities))

    # Optionally save results
    if output_path:
        with open(output_path, 'w') as f:
            json.dump({
                'current_holdings': holdings,
                'opportunities': all_opportunities,
            }, f, indent=2)
        logger.info("Results saved to %s", output_path)

    return all_opportunities
