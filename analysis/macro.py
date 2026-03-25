"""
Macro-economic regime detection using FRED data.

Analyzes macroeconomic conditions to adjust position sizing and risk
exposure based on yield curve, VIX, credit spreads, Fed Funds rate,
and unemployment.

Requires: fredapi library and FRED API key
  (free from https://fred.stlouisfed.org/docs/api/api_key.html)
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from core.config import FRED_API_KEY, REGIME_POSITION_MODIFIERS, VIX_THRESHOLDS

# Optional pandas for data manipulation
try:
    import pandas as pd  # noqa: F401

    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False

# FRED API - optional dependency
try:
    from fredapi import Fred

    HAS_FRED = True
except ImportError:
    HAS_FRED = False

logger = logging.getLogger(__name__)


class MacroAgent:
    """
    Market Regime Detector using FRED macroeconomic data.

    Determines overall market conditions and provides risk modifiers
    to adjust position sizing based on macroeconomic environment.

    Risk Modifiers:
    - 1.0 = Normal conditions, full position sizes
    - 0.75 = Caution, reduce exposure
    - 0.5 = Elevated risk, half positions
    - 0.25 = High risk, minimal exposure
    - 0.0 = Critical, cash only
    """

    # Regime definitions
    REGIME_BULLISH = "BULLISH"
    REGIME_NEUTRAL = "NEUTRAL"
    REGIME_CAUTIOUS = "CAUTIOUS"
    REGIME_BEARISH = "BEARISH"
    REGIME_CRITICAL = "CRITICAL"

    # Thresholds (pulled from centralised config)
    VIX_LOW: int = VIX_THRESHOLDS["low"]
    VIX_ELEVATED: int = VIX_THRESHOLDS["normal"]
    VIX_HIGH: int = VIX_THRESHOLDS["elevated"]
    VIX_EXTREME: int = VIX_THRESHOLDS["extreme"]

    CREDIT_SPREAD_NORMAL: float = 3.5
    CREDIT_SPREAD_ELEVATED: float = 5.0
    CREDIT_SPREAD_CRISIS: float = 7.0

    def __init__(self, api_key: Optional[str] = None) -> None:
        """
        Initialize MacroAgent.

        Args:
            api_key: FRED API key.  If not provided, falls back to
                     ``core.config.FRED_API_KEY``.
        """
        self.api_key: str = api_key or FRED_API_KEY
        self.fred: Optional["Fred"] = None
        self._cache: Dict[str, float] = {}
        self._cache_timestamp: Optional[datetime] = None
        self._cache_duration: timedelta = timedelta(hours=1)

        if HAS_FRED and self.api_key:
            try:
                self.fred = Fred(api_key=self.api_key)
                logger.info("FRED API initialized successfully")
            except Exception as e:
                logger.warning("Failed to initialize FRED API: %s", e)
                self.fred = None
        elif not HAS_FRED:
            logger.warning("fredapi not installed. Run: pip install fredapi")
        elif not self.api_key:
            logger.warning("No FRED API key provided. Set FRED_API_KEY env var.")

    def _is_cache_valid(self) -> bool:
        """Check if cached data is still valid."""
        if not self._cache_timestamp:
            return False
        return datetime.now() - self._cache_timestamp < self._cache_duration

    def _fetch_series(
        self, series_id: str, fallback: Optional[float] = None
    ) -> Optional[float]:
        """
        Fetch latest value from a FRED series.

        Args:
            series_id: FRED series identifier
            fallback: Value to return if fetch fails

        Returns:
            Latest value or fallback
        """
        if not self.fred:
            return fallback

        try:
            # Check cache first
            if self._is_cache_valid() and series_id in self._cache:
                return self._cache[series_id]

            series = self.fred.get_series(series_id)
            if series is not None and len(series) > 0:
                # Get most recent non-NaN value
                latest = series.dropna().iloc[-1]
                self._cache[series_id] = float(latest)
                self._cache_timestamp = datetime.now()
                return float(latest)
        except Exception as e:
            logger.warning("Failed to fetch %s: %s", series_id, e)

        return fallback

    def get_yield_curve(self) -> Tuple[Optional[float], str]:
        """
        Get 10Y-2Y Treasury spread (yield curve).

        Returns:
            Tuple of (spread value, interpretation)
        """
        spread = self._fetch_series("T10Y2Y")

        if spread is None:
            return None, "Data unavailable"

        if spread < -0.5:
            interpretation = "DEEPLY INVERTED - Strong recession signal"
        elif spread < 0:
            interpretation = "INVERTED - Recession warning"
        elif spread < 0.5:
            interpretation = "FLAT - Economic uncertainty"
        elif spread < 1.5:
            interpretation = "NORMAL - Healthy economy"
        else:
            interpretation = "STEEP - Growth expectations high"

        return round(spread, 3), interpretation

    def get_vix(self) -> Tuple[Optional[float], str]:
        """
        Get VIX volatility index.

        Returns:
            Tuple of (VIX value, interpretation)
        """
        vix = self._fetch_series("VIXCLS")

        if vix is None:
            return None, "Data unavailable"

        if vix < self.VIX_LOW:
            interpretation = "LOW - Market complacent (contrarian warning)"
        elif vix < self.VIX_ELEVATED:
            interpretation = "NORMAL - Typical volatility"
        elif vix < self.VIX_HIGH:
            interpretation = "ELEVATED - Increased uncertainty"
        elif vix < self.VIX_EXTREME:
            interpretation = "HIGH - Significant fear"
        else:
            interpretation = "EXTREME - Panic levels"

        return round(vix, 2), interpretation

    def get_credit_spread(self) -> Tuple[Optional[float], str]:
        """
        Get BofA High Yield spread (credit stress indicator).

        Returns:
            Tuple of (spread value, interpretation)
        """
        spread = self._fetch_series("BAMLH0A0HYM2")

        if spread is None:
            return None, "Data unavailable"

        if spread < self.CREDIT_SPREAD_NORMAL:
            interpretation = "TIGHT - Credit conditions easy"
        elif spread < self.CREDIT_SPREAD_ELEVATED:
            interpretation = "NORMAL - Typical credit conditions"
        elif spread < self.CREDIT_SPREAD_CRISIS:
            interpretation = "ELEVATED - Credit stress emerging"
        else:
            interpretation = "CRISIS - Credit crunch conditions"

        return round(spread, 3), interpretation

    def get_fed_funds_rate(self) -> Tuple[Optional[float], str]:
        """
        Get effective Federal Funds Rate.

        Returns:
            Tuple of (rate value, interpretation)
        """
        rate = self._fetch_series("DFF")

        if rate is None:
            return None, "Data unavailable"

        if rate < 1.0:
            interpretation = "ACCOMMODATIVE - Easy money policy"
        elif rate < 3.0:
            interpretation = "NEUTRAL - Balanced policy"
        elif rate < 5.0:
            interpretation = "RESTRICTIVE - Tight policy"
        else:
            interpretation = "VERY RESTRICTIVE - Aggressive tightening"

        return round(rate, 3), interpretation

    def get_unemployment(self) -> Tuple[Optional[float], str]:
        """
        Get unemployment rate.

        Returns:
            Tuple of (rate value, interpretation)
        """
        rate = self._fetch_series("UNRATE")

        if rate is None:
            return None, "Data unavailable"

        if rate < 4.0:
            interpretation = "STRONG - Tight labor market"
        elif rate < 5.0:
            interpretation = "HEALTHY - Normal employment"
        elif rate < 6.5:
            interpretation = "SOFTENING - Labor market weakening"
        else:
            interpretation = "WEAK - Elevated unemployment"

        return round(rate, 2), interpretation

    def get_market_regime(self) -> Dict:
        """
        Determine overall market regime based on all indicators.

        Returns:
            Dict with regime, risk_modifier, and detailed analysis
        """
        # Fetch all indicators
        yield_curve, yield_interp = self.get_yield_curve()
        vix, vix_interp = self.get_vix()
        credit_spread, credit_interp = self.get_credit_spread()
        fed_rate, fed_interp = self.get_fed_funds_rate()
        unemployment, unemp_interp = self.get_unemployment()

        # Track warning signals
        warnings: List[str] = []
        positives: List[str] = []

        # Scoring system
        risk_score: float = 0  # Higher = more risk, range roughly -2 to +10

        # Yield curve analysis (most important recession predictor)
        if yield_curve is not None:
            if yield_curve < -0.5:
                risk_score += 3
                warnings.append("Deeply inverted yield curve - recession signal")
            elif yield_curve < 0:
                risk_score += 2
                warnings.append("Inverted yield curve - caution warranted")
            elif yield_curve < 0.5:
                risk_score += 1
                warnings.append("Flat yield curve - uncertainty")
            else:
                positives.append("Healthy yield curve")

        # VIX analysis
        if vix is not None:
            if vix >= self.VIX_EXTREME:
                risk_score += 3
                warnings.append(f"VIX at panic levels ({vix})")
            elif vix >= self.VIX_HIGH:
                risk_score += 2
                warnings.append(f"VIX elevated ({vix})")
            elif vix >= self.VIX_ELEVATED:
                risk_score += 1
                warnings.append(f"VIX above normal ({vix})")
            elif vix < self.VIX_LOW:
                # Very low VIX can be contrarian warning
                risk_score += 0.5
                warnings.append(f"VIX very low ({vix}) - complacency risk")
            else:
                positives.append(f"VIX normal ({vix})")

        # Credit spread analysis
        if credit_spread is not None:
            if credit_spread >= self.CREDIT_SPREAD_CRISIS:
                risk_score += 3
                warnings.append(
                    f"Credit spreads at crisis levels ({credit_spread})"
                )
            elif credit_spread >= self.CREDIT_SPREAD_ELEVATED:
                risk_score += 2
                warnings.append(f"Credit spreads elevated ({credit_spread})")
            else:
                positives.append("Credit conditions stable")

        # Fed policy (context, not direct risk)
        if fed_rate is not None and fed_rate >= 5.0:
            risk_score += 1
            warnings.append(f"Very restrictive Fed policy ({fed_rate}%)")

        # Unemployment (lagging indicator but important)
        if unemployment is not None and unemployment >= 5.5:
            risk_score += 1
            warnings.append(f"Unemployment rising ({unemployment}%)")

        # Determine regime and risk modifier
        if risk_score >= 7:
            regime = self.REGIME_CRITICAL
            risk_modifier = REGIME_POSITION_MODIFIERS.get("CRITICAL", 0.0)
            recommendation = "CASH ONLY - Multiple severe warning signals"
        elif risk_score >= 5:
            regime = self.REGIME_BEARISH
            risk_modifier = REGIME_POSITION_MODIFIERS.get("BEARISH", 0.25)
            recommendation = "MINIMAL EXPOSURE - Significant macro headwinds"
        elif risk_score >= 3:
            regime = self.REGIME_CAUTIOUS
            risk_modifier = REGIME_POSITION_MODIFIERS.get("CAUTIOUS", 0.5)
            recommendation = "REDUCED POSITIONS - Elevated risk environment"
        elif risk_score >= 1.5:
            regime = self.REGIME_NEUTRAL
            risk_modifier = REGIME_POSITION_MODIFIERS.get("NEUTRAL", 0.75)
            recommendation = "MODERATE CAUTION - Some warning signs present"
        else:
            regime = self.REGIME_BULLISH
            risk_modifier = REGIME_POSITION_MODIFIERS.get("BULLISH", 1.0)
            recommendation = "FULL POSITIONS - Macro conditions supportive"

        return {
            "regime": regime,
            "risk_modifier": risk_modifier,
            "risk_score": round(risk_score, 2),
            "recommendation": recommendation,
            "timestamp": datetime.now().isoformat(),
            "indicators": {
                "yield_curve": {
                    "value": yield_curve,
                    "interpretation": yield_interp,
                },
                "vix": {
                    "value": vix,
                    "interpretation": vix_interp,
                },
                "credit_spread": {
                    "value": credit_spread,
                    "interpretation": credit_interp,
                },
                "fed_funds_rate": {
                    "value": fed_rate,
                    "interpretation": fed_interp,
                },
                "unemployment": {
                    "value": unemployment,
                    "interpretation": unemp_interp,
                },
            },
            "warnings": warnings,
            "positives": positives,
        }

    def get_position_size_modifier(self) -> float:
        """
        Simple method to get just the risk modifier for position sizing.

        Returns:
            Float between 0.0 and 1.0 to multiply position sizes by
        """
        regime_data = self.get_market_regime()
        return regime_data["risk_modifier"]

    def format_report(self) -> str:
        """
        Generate a formatted text report of current market conditions.

        Returns:
            Formatted string report
        """
        data = self.get_market_regime()

        lines = [
            "=" * 60,
            "MACRO ECONOMIC REGIME REPORT",
            "=" * 60,
            f"Timestamp: {data['timestamp']}",
            "",
            f"REGIME: {data['regime']}",
            f"RISK MODIFIER: {data['risk_modifier']} (multiply position sizes by this)",
            f"RISK SCORE: {data['risk_score']} (0=safe, 10=critical)",
            "",
            f"RECOMMENDATION: {data['recommendation']}",
            "",
            "-" * 60,
            "INDICATORS:",
            "-" * 60,
        ]

        for name, info in data["indicators"].items():
            display_name = name.replace("_", " ").title()
            value = info["value"] if info["value"] is not None else "N/A"
            lines.append(f"  {display_name}: {value}")
            lines.append(f"    -> {info['interpretation']}")

        if data["warnings"]:
            lines.extend(
                [
                    "",
                    "-" * 60,
                    "WARNING SIGNALS:",
                    "-" * 60,
                ]
            )
            for warning in data["warnings"]:
                lines.append(f"  ! {warning}")

        if data["positives"]:
            lines.extend(
                [
                    "",
                    "-" * 60,
                    "POSITIVE SIGNALS:",
                    "-" * 60,
                ]
            )
            for positive in data["positives"]:
                lines.append(f"  + {positive}")

        lines.append("=" * 60)

        return "\n".join(lines)


def get_macro_regime() -> Dict:
    """
    Convenience function to get market regime without instantiating class.

    Returns:
        Dict with regime info or error message
    """
    try:
        agent = MacroAgent()
        if agent.fred is None:
            return {
                "error": "FRED API not available",
                "regime": "UNKNOWN",
                "risk_modifier": 0.75,  # Default to cautious if no data
                "recommendation": "Unable to fetch macro data - proceeding with caution",
            }
        return agent.get_market_regime()
    except Exception as e:
        return {
            "error": str(e),
            "regime": "UNKNOWN",
            "risk_modifier": 0.75,
            "recommendation": f"Macro analysis failed: {e}",
        }
