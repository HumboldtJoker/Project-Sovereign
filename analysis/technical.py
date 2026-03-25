"""
Technical indicators: SMA, RSI, MACD, Bollinger Bands.

All indicators use yfinance for historical data.
"""

import logging
from datetime import datetime
from typing import Dict

import numpy as np
import pandas as pd
import yfinance as yf

from core.config import (
    BOLLINGER_EXPANSION_THRESHOLD,
    BOLLINGER_SQUEEZE_THRESHOLD,
    RSI_OVERBOUGHT,
    RSI_OVERSOLD,
)

logger = logging.getLogger(__name__)

BOLLINGER_NARROW_BANDWIDTH = BOLLINGER_SQUEEZE_THRESHOLD * 100  # 10
BOLLINGER_WIDE_BANDWIDTH = BOLLINGER_EXPANSION_THRESHOLD * 100   # 20


def get_technical_indicators(ticker: str, period: str = "6mo") -> Dict:
    """Calculate comprehensive technical indicators for a stock."""
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period=period)
        if hist.empty:
            return {"error": f"No historical data available for {ticker}"}

        sma_data = calculate_sma(hist)
        rsi_data = calculate_rsi(hist)
        macd_data = calculate_macd(hist)
        bb_data = calculate_bollinger_bands(hist)

        current_price = hist["Close"].iloc[-1]

        return {
            "ticker": ticker,
            "current_price": round(current_price, 2),
            "period": period,
            "data_points": len(hist),
            "latest_date": hist.index[-1].strftime("%Y-%m-%d"),
            "sma": sma_data,
            "rsi": rsi_data,
            "macd": macd_data,
            "bollinger_bands": bb_data,
            "overall_signal": _generate_overall_signal(sma_data, rsi_data, macd_data, bb_data),
        }
    except Exception as e:
        logger.error("Technical analysis failed for %s: %s", ticker, e)
        return {"error": f"Failed to calculate technical indicators: {e}"}


def calculate_sma(hist: pd.DataFrame, periods: list = None) -> Dict:
    """Calculate Simple Moving Averages."""
    if periods is None:
        periods = [20, 50, 200]
    close = hist["Close"]
    current_price = close.iloc[-1]
    smas = {}

    for period in periods:
        if len(close) >= period:
            sma_value = close.rolling(window=period).mean().iloc[-1]
            smas[f"sma_{period}"] = round(sma_value, 2)
            distance = ((current_price - sma_value) / sma_value) * 100
            smas[f"sma_{period}_distance"] = round(distance, 2)

    signals = []
    if "sma_50" in smas and "sma_200" in smas:
        if smas["sma_50"] > smas["sma_200"]:
            signals.append("Golden Cross: 50-day SMA above 200-day SMA (bullish)")
        else:
            signals.append("Death Cross: 50-day SMA below 200-day SMA (bearish)")

    if "sma_50" in smas:
        above = current_price > smas["sma_50"]
        signals.append(f"Price {'above' if above else 'below'} 50-day SMA ({'bullish' if above else 'bearish'})")

    if "sma_200" in smas:
        above = current_price > smas["sma_200"]
        signals.append(f"Price {'above' if above else 'below'} 200-day SMA (long-term {'bullish' if above else 'bearish'})")

    smas["signals"] = signals
    return smas


def calculate_rsi(hist: pd.DataFrame, period: int = 14) -> Dict:
    """Calculate Relative Strength Index."""
    close = hist["Close"]
    delta = close.diff()
    gains = delta.where(delta > 0, 0)
    losses = -delta.where(delta < 0, 0)
    avg_gains = gains.rolling(window=period).mean()
    avg_losses = losses.rolling(window=period).mean()
    avg_losses_safe = avg_losses.replace(0, 1e-10)
    rs = avg_gains / avg_losses_safe
    rsi = 100 - (100 / (1 + rs))
    current_rsi = rsi.iloc[-1]

    if pd.isna(current_rsi):
        return {"error": f"Insufficient data for RSI calculation (need {period}+ days)"}

    if current_rsi >= RSI_OVERBOUGHT:
        signal = f"Overbought (RSI >= {RSI_OVERBOUGHT}) - potential sell signal"
        sentiment = "bearish"
    elif current_rsi <= RSI_OVERSOLD:
        signal = f"Oversold (RSI <= {RSI_OVERSOLD}) - potential buy signal"
        sentiment = "bullish"
    elif current_rsi > 50:
        signal = "Bullish momentum (RSI > 50)"
        sentiment = "bullish"
    else:
        signal = "Bearish momentum (RSI < 50)"
        sentiment = "bearish"

    return {"rsi_14": round(current_rsi, 2), "signal": signal, "sentiment": sentiment}


def calculate_macd(hist: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9) -> Dict:
    """Calculate MACD."""
    close = hist["Close"]
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line

    current_macd = macd_line.iloc[-1]
    current_signal = signal_line.iloc[-1]
    current_histogram = histogram.iloc[-1]

    signals = []
    if current_macd > current_signal:
        signals.append("MACD above signal line (bullish)")
        sentiment = "bullish"
    else:
        signals.append("MACD below signal line (bearish)")
        sentiment = "bearish"

    if current_histogram > 0:
        signals.append(f"Positive histogram ({round(current_histogram, 2)}) - upward momentum")
    else:
        signals.append(f"Negative histogram ({round(current_histogram, 2)}) - downward momentum")

    # Check for recent crossover
    recent = 5
    macd_recent = macd_line.iloc[-recent:]
    signal_recent = signal_line.iloc[-recent:]
    for i in range(1, len(macd_recent)):
        if macd_recent.iloc[i] > signal_recent.iloc[i] and macd_recent.iloc[i - 1] <= signal_recent.iloc[i - 1]:
            signals.append("RECENT: Bullish crossover detected")
            break
        elif macd_recent.iloc[i] < signal_recent.iloc[i] and macd_recent.iloc[i - 1] >= signal_recent.iloc[i - 1]:
            signals.append("RECENT: Bearish crossover detected")
            break

    return {
        "macd": round(current_macd, 4),
        "signal_line": round(current_signal, 4),
        "histogram": round(current_histogram, 4),
        "signals": signals,
        "sentiment": sentiment,
    }


def calculate_bollinger_bands(hist: pd.DataFrame, period: int = 20, std_dev: int = 2) -> Dict:
    """Calculate Bollinger Bands."""
    close = hist["Close"]
    middle_band = close.rolling(window=period).mean()
    std = close.rolling(window=period).std()
    upper_band = middle_band + (std * std_dev)
    lower_band = middle_band - (std * std_dev)

    current_price = close.iloc[-1]
    current_upper = upper_band.iloc[-1]
    current_middle = middle_band.iloc[-1]
    current_lower = lower_band.iloc[-1]

    bandwidth = ((current_upper - current_lower) / current_middle) * 100
    percent_b = (current_price - current_lower) / (current_upper - current_lower)

    signals = []
    if current_price >= current_upper:
        signals.append("Price at or above upper band - overbought, possible reversal")
        sentiment = "bearish"
    elif current_price <= current_lower:
        signals.append("Price at or below lower band - oversold, possible reversal")
        sentiment = "bullish"
    elif current_price > current_middle:
        signals.append("Price above middle band - bullish trend")
        sentiment = "bullish"
    else:
        signals.append("Price below middle band - bearish trend")
        sentiment = "bearish"

    if bandwidth < BOLLINGER_NARROW_BANDWIDTH:
        signals.append(f"Narrow bandwidth ({round(bandwidth, 2)}%) - low volatility, potential breakout")
    elif bandwidth > BOLLINGER_WIDE_BANDWIDTH:
        signals.append(f"Wide bandwidth ({round(bandwidth, 2)}%) - high volatility")

    return {
        "upper_band": round(current_upper, 2),
        "middle_band": round(current_middle, 2),
        "lower_band": round(current_lower, 2),
        "current_price": round(current_price, 2),
        "bandwidth_pct": round(bandwidth, 2),
        "percent_b": round(percent_b, 2),
        "signals": signals,
        "sentiment": sentiment,
    }


def _generate_overall_signal(sma_data: Dict, rsi_data: Dict, macd_data: Dict, bb_data: Dict) -> Dict:
    """Combine all indicators into an overall signal."""
    bullish = 0
    bearish = 0
    total = 0

    for signal in sma_data.get("signals", []):
        total += 1
        if "bullish" in signal.lower():
            bullish += 1
        elif "bearish" in signal.lower():
            bearish += 1

    for data in (rsi_data, macd_data, bb_data):
        total += 1
        if data.get("sentiment") == "bullish":
            bullish += 1
        else:
            bearish += 1

    bullish_pct = (bullish / total) * 100 if total else 50
    bearish_pct = (bearish / total) * 100 if total else 50

    if bullish_pct >= 70:
        recommendation, confidence = "STRONG BUY", "high"
    elif bullish_pct >= 55:
        recommendation, confidence = "BUY", "moderate"
    elif bearish_pct >= 70:
        recommendation, confidence = "STRONG SELL", "high"
    elif bearish_pct >= 55:
        recommendation, confidence = "SELL", "moderate"
    else:
        recommendation, confidence = "HOLD", "low"

    return {
        "recommendation": recommendation,
        "confidence": confidence,
        "bullish_signals": bullish,
        "bearish_signals": bearish,
        "total_signals": total,
        "bullish_pct": round(bullish_pct, 1),
        "bearish_pct": round(bearish_pct, 1),
    }
