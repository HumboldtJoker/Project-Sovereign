"""Sovereign Pipeline — CC's autonomous market intelligence.

Connects data sources → Claude analysis → trading thesis → paper execution.
Built for a $100 account. Every dollar matters. Discipline over ambition.

Data: Alpaca (quotes, bars, portfolio), FRED (macro), Congressional (when available)
Analysis: Claude Sonnet for thesis generation
Execution: Alpaca paper trading API

Usage:
    python3 sovereign_pipeline.py scan          # Scan for opportunities
    python3 sovereign_pipeline.py portfolio     # Review current positions
    python3 sovereign_pipeline.py thesis NVDA   # Generate thesis for a ticker
    python3 sovereign_pipeline.py execute       # Execute on highest-conviction thesis
"""

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("sovereign")

# Keys come from the environment only (see sovereign_config) — no fallbacks.
from sovereign_config import (RESULTS_DIR, RISK, THESIS_MODEL, alpaca_keys,
                              anthropic_key, fred_key, with_timeout)
from risk_engine import enforce_stop_rule, rule_stop

ALPACA_KEY, ALPACA_SECRET, ALPACA_PAPER = alpaca_keys()

# The Claude CLI used for OAuth theses. Override with SOVEREIGN_CLAUDE_CLI;
# the default is whatever `claude` is on PATH, then the usual install spot.
import shutil as _shutil
CLAUDE_CLI = (os.environ.get("SOVEREIGN_CLAUDE_CLI")
              or _shutil.which("claude")
              or str(Path.home() / ".local/bin/claude"))


def _num(x, default: float = 0.0) -> float:
    """A model-supplied number, or `default` if it isn't one ("$250", "n/a")."""
    try:
        return float(str(x).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return default


@dataclass
class MarketContext:
    """Current market conditions for analysis."""
    vix: float = 0.0
    fed_funds_rate: float = 0.0
    macro_regime: str = "unknown"
    market_open: bool = False
    timestamp: str = ""


@dataclass
class StockData:
    """Data package for a single ticker."""
    ticker: str
    current_price: float = 0.0
    bars_5d: list = field(default_factory=list)
    bars_30d: list = field(default_factory=list)
    volume_avg: float = 0.0
    price_change_5d: float = 0.0
    price_change_30d: float = 0.0
    rsi_14: float = 50.0
    atr_14: float = 0.0
    congressional: dict = field(default_factory=dict)


@dataclass
class Thesis:
    """A trading thesis from Claude analysis."""
    ticker: str
    direction: str  # "buy" | "sell" | "hold"
    conviction: str  # "high" | "medium" | "low"
    position_size_pct: float = 0.0
    entry_price: float = 0.0
    stop_loss: float = 0.0
    target: float = 0.0
    reasoning: str = ""
    risk_factors: list = field(default_factory=list)
    timestamp: str = ""


def get_macro_context() -> MarketContext:
    """Pull current macro conditions from FRED + Alpaca."""
    from alpaca.trading.client import TradingClient

    ctx = MarketContext(timestamp=datetime.now().isoformat())

    try:
        client = TradingClient(ALPACA_KEY, ALPACA_SECRET, paper=ALPACA_PAPER)
        clock = client.get_clock()
        ctx.market_open = clock.is_open
    except Exception as e:
        log.warning("Alpaca clock check failed: %s", e)

    try:
        from fredapi import Fred
        fred = Fred(api_key=fred_key())

        vix = fred.get_series("VIXCLS", observation_start=(datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d"))
        if not vix.empty:
            ctx.vix = float(vix.dropna().iloc[-1])

        ffr = fred.get_series("FEDFUNDS", observation_start="2026-01-01")
        if not ffr.empty:
            ctx.fed_funds_rate = float(ffr.dropna().iloc[-1])

    except Exception as e:
        log.warning("FRED data failed: %s", e)

    if ctx.vix < 15:
        ctx.macro_regime = "BULLISH"
    elif ctx.vix < 25:
        ctx.macro_regime = "NEUTRAL"
    elif ctx.vix < 35:
        ctx.macro_regime = "CAUTIOUS"
    else:
        ctx.macro_regime = "CRITICAL"

    return ctx


MAX_SPREAD_FOR_MID = 0.01


def live_price(client, ticker: str, fallback: float, TradeReq, QuoteReq) -> float:
    """Price the thesis anchors to while the market is open.

    It used to be the latest IEX ASK. Most scans run in the first minutes
    after the open, when IEX spreads are 5-12%, so the recorded entry sat a
    median 2.6% (up to 8.7%) above what the order actually paid -- and every
    stop and target measured from it was that much off. Order of preference:
    the latest trade; the quote midpoint if the spread is under 1%; the last
    daily close. Never the ask alone.
    """
    try:
        t = client.get_stock_latest_trade(TradeReq(symbol_or_symbols=[ticker]))
        if ticker in t and float(t[ticker].price) > 0 and _fresh_regular_session(t[ticker].timestamp):
            return float(t[ticker].price)
    except Exception as e:
        log.warning("Latest trade failed for %s: %s", ticker, e)
    try:
        q = client.get_stock_latest_quote(QuoteReq(symbol_or_symbols=[ticker]))
        if ticker in q:
            bid, ask = float(q[ticker].bid_price), float(q[ticker].ask_price)
            if bid > 0 and ask > 0 and (ask - bid) / ((ask + bid) / 2) < MAX_SPREAD_FOR_MID:
                return round((ask + bid) / 2, 4)
            log.info("%s spread too wide for a midpoint (bid %.2f ask %.2f); using last close",
                     ticker, bid, ask)
    except Exception as e:
        log.warning("Quote failed for %s: %s", ticker, e)
    return fallback


def _fresh_regular_session(ts, max_age_min: float = 15.0) -> bool:
    """A latest trade counts only if it printed today after the 09:30 ET open
    and within the last few minutes. On IEX a thin name's "latest trade" at
    the open can be yesterday's after-hours print or a days-old one."""
    from zoneinfo import ZoneInfo
    if ts is None:
        return False
    et = ZoneInfo("America/New_York")
    now = datetime.now(et)
    ts = ts.astimezone(et) if ts.tzinfo else ts.replace(tzinfo=et)
    open_today = now.replace(hour=9, minute=30, second=0, microsecond=0)
    return open_today <= ts <= now + timedelta(minutes=1) and (now - ts) <= timedelta(minutes=max_age_min)


def get_stock_data(ticker: str, market_open: bool = False) -> StockData:
    """Pull price data and technicals for a ticker."""
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.enums import Adjustment
    from alpaca.data.requests import (StockBarsRequest, StockLatestQuoteRequest,
                                      StockLatestTradeRequest)
    from alpaca.data.timeframe import TimeFrame

    data = StockData(ticker=ticker)
    client = with_timeout(StockHistoricalDataClient(ALPACA_KEY, ALPACA_SECRET))

    try:
        # No `end`: the API then defaults to now-15min on this data plan. An
        # explicit end=now is refused (403, "recent SIP data") and the old end
        # of "yesterday" was sent as 00:00Z yesterday, dropping yesterday's bar.
        # Today's forming bar is dropped below.
        start_dt = datetime.now(timezone.utc) - timedelta(days=36)
        bars_req = StockBarsRequest(
            symbol_or_symbols=[ticker],
            timeframe=TimeFrame.Day,
            start=start_dt.strftime("%Y-%m-%d"),
            adjustment=Adjustment.ALL,   # raw bars turned a 4:1 split into a "crash"
        )
        bars = client.get_stock_bars(bars_req)
        if bars[ticker]:
            from zoneinfo import ZoneInfo
            today_et = datetime.now(ZoneInfo("America/New_York")).date()
            bar_list = [b for b in bars[ticker]
                        if b.timestamp.astimezone(ZoneInfo("America/New_York")).date() < today_et]
            data.bars_30d = [{"date": str(b.timestamp.date()), "close": float(b.close),
                              "volume": int(b.volume), "high": float(b.high), "low": float(b.low)}
                             for b in bar_list]
            data.bars_5d = data.bars_30d[-5:]

            if len(data.bars_30d) >= 2:
                data.price_change_30d = (data.bars_30d[-1]["close"] / data.bars_30d[0]["close"] - 1) * 100
            if len(data.bars_5d) >= 2:
                data.price_change_5d = (data.bars_5d[-1]["close"] / data.bars_5d[0]["close"] - 1) * 100
            if data.bars_30d:
                data.volume_avg = sum(b["volume"] for b in data.bars_30d) / len(data.bars_30d)

            closes = [b["close"] for b in data.bars_30d]
            data.rsi_14 = compute_rsi(closes, 14)
            data.atr_14 = compute_atr(data.bars_30d, 14)

            data.current_price = data.bars_30d[-1]["close"]
    except Exception as e:
        log.warning("Bars failed for %s: %s", ticker, e)

    if market_open:
        data.current_price = live_price(client, ticker, data.current_price,
                                        StockLatestTradeRequest, StockLatestQuoteRequest)

    return data


def compute_rsi(closes: list, period: int = 14) -> float:
    """Compute RSI from closing prices."""
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0 for d in deltas[-period:]]
    losses = [-d if d < 0 else 0 for d in deltas[-period:]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def compute_atr(bars: list, period: int = 14) -> float:
    """Compute Average True Range from bar dicts (keys: high, low, close).

    True Range = max(high-low, abs(high-prev_close), abs(low-prev_close)).
    ATR = simple average of True Range over `period` bars.
    Requires at least period+1 bars (need a previous close for the first TR).
    """
    if len(bars) < period + 1:
        return 0.0
    true_ranges = []
    for i in range(1, len(bars)):
        h = bars[i]["high"]
        l = bars[i]["low"]
        prev_c = bars[i - 1]["close"]
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        true_ranges.append(tr)
    # Use the last `period` true ranges
    return sum(true_ranges[-period:]) / period


def _get_portfolio_context() -> str:
    """Read current positions and format as context for the LLM."""
    from sovereign_config import STATE_DIR, load_json
    positions = load_json(STATE_DIR / "positions_state.json", default={})
    if not positions:
        return "\nPORTFOLIO: No open positions. Full cash available.\n"

    lines = [f"\nCURRENT PORTFOLIO ({len(positions)} open positions):"]
    for ticker, pos in positions.items():
        entry = pos.get("entry", 0)
        stop = pos.get("stop", 0)
        target = pos.get("target", 0)
        lines.append(f"  {ticker}: entry ${entry:.2f}, stop ${stop:.2f}, target ${target:.2f}")

    pct_deployed = len(positions) * 15  # rough estimate at 15% per position
    lines.append(f"Approximate deployment: ~{pct_deployed}% of equity")
    if len(positions) >= 4:
        lines.append("WARNING: At or near max positions. Only enter if clearly better than an existing holding.")
    lines.append("")
    return "\n".join(lines)


def generate_thesis(ticker: str, stock_data: StockData, macro: MarketContext,
                     sector_context: str = "", congress_context: str = "",
                     signal_context: str = "") -> Thesis:
    """Ask Claude to write/veto a thesis around the composite signal.

    PolyBench lesson: the LLM does not originate conviction — the structured
    composite does. Claude's job is narrative, sanity-checking, and veto.
    """
    # SOVEREIGN_THESIS_BACKEND: "auto" (default; SDK if ANTHROPIC_API_KEY is
    # set, else the Claude CLI), "cli" (subscription OAuth only), or "sdk".
    # Before this switch, a deployment with an unfunded key made a doomed SDK
    # call ahead of every CLI thesis.
    backend = os.environ.get("SOVEREIGN_THESIS_BACKEND", "auto").lower()
    _use_cli, client = True, None
    if backend in ("auto", "sdk"):
        try:
            anthropic_key()
            import anthropic
            client = anthropic.Anthropic(api_key=anthropic_key())
            _use_cli = False
        except Exception:
            if backend == "sdk":
                log.error("SOVEREIGN_THESIS_BACKEND=sdk but no usable ANTHROPIC_API_KEY")
                return None

    sector = SECTOR_MAP.get(ticker, "Unknown")
    sector_block = ""
    if sector_context:
        sector_block = f"""
SECTOR ROTATION (current 5-day performance across scanned universe):
{sector_context}
This stock is in: {sector}. Consider whether money is flowing INTO or OUT OF this sector.
"""

    congress_block = ""
    if congress_context:
        congress_block = f"""
{congress_context}
Congressional trading is a SUPPORTING signal, not primary. Weight it alongside technicals.
Multiple members buying the same stock within 30 days is a stronger signal than a single trade.
"""

    signal_block = ""
    if signal_context:
        signal_block = f"""
QUANTITATIVE COMPOSITE SIGNAL (deterministic, multi-source):
{signal_context}
Your role: sanity-check and narrate this signal, or VETO it if the price action
tells a different story. Do not manufacture conviction the composite lacks —
if the composite is weak and you see nothing compelling, say hold.
"""

    portfolio_block = _get_portfolio_context()

    memory_block = ""
    try:
        from sovereign_memory import SovereignMemory
        mem = SovereignMemory()
        memory_block = mem.recall_for_thesis(ticker, SECTOR_MAP.get(ticker, ""))
        mem.close()
    except Exception as e:
        log.debug("Memory recall unavailable: %s", e)

    prompt = f"""You are a disciplined market analyst for a small autonomous trading account.
All position sizing is expressed as PERCENT of account equity — the account uses
fractional shares, so any stock is tradeable regardless of share price or account size.
Conservative position sizing. No speculation without evidence.
{portfolio_block}
{memory_block}
CURRENT MACRO:
- VIX: {macro.vix:.1f} (regime: {macro.macro_regime})
- Fed Funds Rate: {macro.fed_funds_rate:.2f}%
- Market: {"OPEN" if macro.market_open else "CLOSED"}
{sector_block}{congress_block}{signal_block}
STOCK: {ticker} ({sector})
- Current price: ${stock_data.current_price:.2f}
- 5-day change: {stock_data.price_change_5d:+.1f}%
- 30-day change: {stock_data.price_change_30d:+.1f}%
- RSI(14): {stock_data.rsi_14:.1f}
- ATR(14): ${stock_data.atr_14:.2f} (14-day Average True Range — use this to gauge volatility)
- Avg daily volume: {stock_data.volume_avg:,.0f}
- Recent bars (last 5 days): {json.dumps(stock_data.bars_5d, indent=2)}

POSITION SIZING RULES:
- Max 4 open positions at a time. If already at 4, say "hold" unless this is clearly
  better than an existing position (in which case, say which one to close).
- Max position: 30% of portfolio
- High conviction: 25%
- Medium conviction: 15%
- Low conviction: 10%
- Stop loss: 2x ATR below entry (minimum 8% below entry as floor)
- Cash reserve: maintain 10% minimum

Respond in EXACTLY this JSON format:
{{
    "direction": "buy" or "sell" or "hold",
    "conviction": "high" or "medium" or "low" or "none",
    "position_size_pct": 0-30,
    "entry_price": current price or limit price,
    "stop_loss": price for stop loss,
    "target": price target,
    "reasoning": "2-3 sentence thesis",
    "risk_factors": ["risk1", "risk2"]
}}

If the setup isn't compelling, say "hold" with conviction "none".
Better to miss an opportunity than force a bad trade."""

    text = None

    if not _use_cli and client:
        try:
            import anthropic
            resp = client.messages.create(
                model=THESIS_MODEL,
                max_tokens=500,
                messages=[{"role": "user", "content": prompt}],
            )
            text = resp.content[0].text.strip()
            log.info("Thesis via Anthropic SDK (%s)", THESIS_MODEL)
        except Exception as e:
            log.warning("Anthropic SDK failed (%s), trying Claude CLI", e)

    if text is None:
        import subprocess
        try:
            cli_env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
            result = subprocess.run(
                [CLAUDE_CLI, "-p", "--model", "sonnet", prompt],
                capture_output=True, text=True, timeout=120,
                env=cli_env,
            )
            if result.returncode == 0 and result.stdout.strip():
                text = result.stdout.strip()
                log.info("Thesis via Claude CLI (OAuth)")
            else:
                # stdout too: the CLI reports usage limits and auth errors there,
                # and stderr is usually empty.
                log.error("Claude CLI failed: rc=%d stderr=%r stdout=%r", result.returncode,
                          result.stderr[:300], result.stdout[:300])
                return None
        except Exception as e:
            log.error("Claude CLI unavailable (%s) — using composite-only fallback", e)
            return None
    try:
        start = text.index("{")
        end = text.rindex("}") + 1
        data = json.loads(text[start:end])
    except (ValueError, json.JSONDecodeError) as e:
        log.error("Failed to parse Claude response: %s\n%s", e, text[:200])
        return Thesis(ticker=ticker, direction="hold", conviction="none",
                      reasoning="Analysis failed to parse", timestamp=datetime.now().isoformat())

    # A non-numeric price from the model used to raise here, uncaught, and
    # abort the whole scan after its signals were already written.
    entry = _num(data.get("entry_price")) or stock_data.current_price
    stop, note = enforce_stop_rule(entry, _num(data.get("stop_loss")), stock_data.atr_14)
    risks = list(data.get("risk_factors") or [])
    if note and (data.get("direction") or "").lower() == "buy":
        log.info("%s: %s", ticker, note)
        risks.append(f"[code] {note}")
    target = _num(data.get("target"))
    if target <= entry:
        target = round(entry * (1 + RISK["target_pct"]), 2)
    return Thesis(
        ticker=ticker,
        direction=data.get("direction", "hold") or "hold",
        conviction=data.get("conviction", "none") or "none",
        position_size_pct=_num(data.get("position_size_pct")),
        entry_price=entry,
        stop_loss=stop,
        target=target,
        reasoning=data.get("reasoning") or "",
        risk_factors=risks,
        timestamp=datetime.now().isoformat(),
    )


def fallback_thesis(ticker: str, stock: StockData, sig) -> Thesis:
    """Hold when Claude is unreachable — composite alone is not reliable enough.

    The LLM veto layer exists to catch cases the composite misses (sector
    exhaustion, stale signals, falling knives). Without it, every composite-
    only entry in July 2026 hit its stop. Changed from demote-and-trade to
    hold on 2026-07-28 after every fallback trade stopped out.
    """
    from sovereign_config import RISK
    conviction = "none"
    direction = "hold"
    size = 0
    entry = stock.current_price
    # ATR-based stop with fixed 8% as floor (prevents too-tight stops on low-vol names)
    stop = rule_stop(entry, stock.atr_14)
    return Thesis(
        ticker=ticker, direction=direction, conviction=conviction,
        position_size_pct=size, entry_price=entry,
        stop_loss=stop,
        target=round(entry * (1 + RISK["target_pct"]), 2),
        reasoning="[composite-only fallback — LLM veto layer unavailable] " + sig.explain(),
        risk_factors=list(sig.risk_flags) + ["No LLM sanity-check on this thesis"],
        timestamp=datetime.now().isoformat(),
    )


def get_portfolio():
    """Get current portfolio state."""
    from alpaca.trading.client import TradingClient

    client = TradingClient(ALPACA_KEY, ALPACA_SECRET, paper=ALPACA_PAPER)
    account = client.get_account()
    positions = client.get_all_positions()

    print(f"\n{'='*60}")
    print(f"SOVEREIGN PORTFOLIO — {'PAPER' if ALPACA_PAPER else 'LIVE'}")
    print(f"{'='*60}")
    print(f"Portfolio value: ${float(account.portfolio_value):.2f}")
    print(f"Cash:           ${float(account.cash):.2f}")
    print(f"Buying power:   ${float(account.buying_power):.2f}")

    if positions:
        print(f"\nPositions ({len(positions)}):")
        for p in positions:
            pnl = float(p.unrealized_pl)
            pnl_pct = float(p.unrealized_plpc) * 100
            print(f"  {p.symbol:6s} {float(p.qty):>10.4f} shares "
                  f"@ ${float(p.avg_entry_price):>8.2f} → ${float(p.current_price):>8.2f} "
                  f"| P/L: ${pnl:>7.2f} ({pnl_pct:>+6.1f}%)")
    else:
        print("\nNo open positions")
    print()
    return account, positions


SECTOR_MAP = {
    "AAPL": "Tech", "GOOGL": "Tech", "META": "Tech", "AMZN": "Tech/Retail",
    "NVDA": "Semiconductors", "MSFT": "Tech", "AMD": "Semiconductors",
    "TSM": "Semiconductors", "AVGO": "Semiconductors", "GEV": "Energy/Industrial",
    "PLTR": "Tech/Defense", "COIN": "Crypto", "TSLA": "EV/Tech",
    "SNOW": "Cloud", "CRWD": "Cybersecurity", "NET": "Cloud", "DDOG": "Cloud",
    "MDB": "Cloud", "PANW": "Cybersecurity", "ZS": "Cybersecurity",
    "SQ": "Fintech", "SHOP": "E-Commerce", "RBLX": "Gaming", "U": "Gaming",
    "ABNB": "Travel", "UBER": "Rideshare", "LYFT": "Rideshare", "DASH": "Delivery",
    "PINS": "Social", "SNAP": "Social", "SOFI": "Fintech", "HOOD": "Fintech",
    "AFRM": "Fintech", "UPST": "Fintech",
    "MARA": "Crypto Mining", "RIOT": "Crypto Mining", "CLSK": "Crypto Mining", "HUT": "Crypto Mining",
    "LLY": "Pharma", "UNH": "Healthcare", "JNJ": "Pharma", "PFE": "Pharma",
    "ABBV": "Pharma", "MRK": "Pharma", "BMY": "Pharma",
    "JPM": "Banks", "GS": "Banks", "BAC": "Banks", "WFC": "Banks",
    "MS": "Banks", "C": "Banks", "V": "Payments", "MA": "Payments",
    "XOM": "Energy", "CVX": "Energy", "COP": "Energy", "OXY": "Energy", "SLB": "Energy",
    "BA": "Defense/Aerospace", "LMT": "Defense", "RTX": "Defense", "GD": "Defense", "NOC": "Defense",
    "DIS": "Media", "NFLX": "Media/Streaming", "CMCSA": "Media", "WBD": "Media",
    # AI IPO wave
    "CRWV": "AI Infrastructure", "ALAB": "Semiconductors", "RDDT": "Social",
    "TEM": "AI Healthcare", "ARM": "Semiconductors", "RBRK": "Cybersecurity",
}


def load_opportunity_candidates(max_age_hours: int = 6) -> list[str]:
    """Pull recent flagged tickers from the opportunity scanner."""
    opp_files = sorted(RESULTS_DIR.glob("opportunity_*.json"), reverse=True)
    candidates = set()
    cutoff = datetime.now() - timedelta(hours=max_age_hours)
    for f in opp_files[:3]:
        try:
            data = json.loads(f.read_text())
            ts = datetime.fromisoformat(data["timestamp"])
            if ts < cutoff:
                continue
            for opp in data.get("opportunities", []):
                if any(flag.startswith("OVERSOLD") or flag.startswith("MOVER") or flag.startswith("VOLUME")
                       for flag in opp.get("flags", [])):
                    candidates.add(opp["ticker"])
        except Exception:
            continue
    return list(candidates)


def build_sector_summary(stocks: dict[str, StockData]) -> str:
    """Summarize sector performance from already-fetched stock data."""
    sectors = {}
    for ticker, data in stocks.items():
        sector = SECTOR_MAP.get(ticker, "Other")
        if sector not in sectors:
            sectors[sector] = []
        sectors[sector].append((ticker, data.price_change_5d, data.rsi_14))

    lines = []
    for sector, tickers in sorted(sectors.items()):
        avg_chg = sum(t[1] for t in tickers) / len(tickers)
        avg_rsi = sum(t[2] for t in tickers) / len(tickers)
        direction = "rallying" if avg_chg > 2 else "selling off" if avg_chg < -2 else "flat"
        lines.append(f"  {sector}: {avg_chg:+.1f}% (5d), avg RSI {avg_rsi:.0f} — {direction}")
    return "\n".join(lines)


def scan_opportunities(watchlist: list[str] = None):
    """Scan a watchlist for opportunities.

    Writes signals_<run_id>.json and scan_<run_id>.json; the scan names its
    snapshot so execute can never pair one run's signals with another's theses.
    """
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    from sovereign_config import AI_IPO_WATCHLIST
    core_watchlist = [
        "AAPL", "GOOGL", "META", "AMZN", "NVDA", "MSFT",
        "GEV", "PLTR", "COIN", "AMD", "TSM", "AVGO",
    ] + list(AI_IPO_WATCHLIST)  # the AI IPO wave is shaping this market
    opp_candidates = load_opportunity_candidates()
    congress_tickers = []
    try:
        from congress_scraper import load_existing_transactions, detect_herds
        txs = load_existing_transactions()
        if txs:
            herds = detect_herds(txs)
            congress_tickers = [s.ticker for s in herds if s.ticker not in core_watchlist]
    except ImportError:
        pass
    if watchlist is None:
        watchlist = list(dict.fromkeys(core_watchlist + opp_candidates + congress_tickers))

    macro = get_macro_context()

    # Multi-factor regime (VIX + curve + credit + trend) supersedes VIX-only
    regime = {}
    try:
        from macro_regime import get_regime
        regime = get_regime()
        macro.macro_regime = regime["regime"]
    except Exception as e:
        log.warning("macro_regime failed, falling back to VIX-only: %s", e)

    print(f"\n{'='*60}")
    print(f"SOVEREIGN SCAN — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}")
    print(f"Macro: VIX={macro.vix:.1f} | regime={macro.macro_regime} "
          f"(exposure x{regime.get('exposure_multiplier', '?')}) | "
          f"FFR={macro.fed_funds_rate:.2f}% | "
          f"Market {'OPEN' if macro.market_open else 'CLOSED'}")

    if macro.macro_regime in ("CRITICAL", "CRISIS"):
        print("\n⚠️  CRISIS regime — no new positions. Cash is king.")
        # Without a fresh (empty) scan, a 10:50 execute would act on the 09:30
        # scan written before the regime turned.
        (RESULTS_DIR / f"scan_{run_id}.json").write_text(json.dumps(
            {"run_id": run_id, "macro": vars(macro), "theses": [],
             "halted": f"{macro.macro_regime} regime — no new positions"}, indent=2, default=str))
        return []

    if opp_candidates:
        print(f"Opportunity candidates added: {', '.join(opp_candidates)}")
    print(f"Scanning {len(watchlist)} tickers")

    stocks = {}
    for ticker in watchlist:
        log.info("Fetching %s...", ticker)
        stock = get_stock_data(ticker, market_open=macro.market_open)
        if stock.current_price == 0:
            log.warning("No price data for %s, skipping", ticker)
            continue
        stocks[ticker] = stock

    sector_summary = build_sector_summary(stocks)
    if sector_summary:
        print(f"\nSector Rotation:")
        print(sector_summary)

    herd_signals = []
    member_weights = {}
    try:
        from congress_scraper import load_existing_transactions, detect_herds, format_herd_for_thesis
        from member_scoring import load_member_weights
        member_weights = load_member_weights()
        congress_txs = load_existing_transactions()
        if congress_txs:
            herd_signals = detect_herds(congress_txs, member_weights=member_weights)
            if herd_signals:
                print(f"\nCongressional Herd Signals: {len(herd_signals)}"
                      f"{' (track-record weighted)' if member_weights else ''}")
                for s in herd_signals[:5]:
                    print(f"  {s.ticker} — {len(s.members)} members {s.direction} ({s.conviction})")
    except ImportError:
        pass

    corr_breaks = {}
    try:
        from correlation_breaks import detect_breaks
        corr_breaks = detect_breaks()
        for ev in corr_breaks.get("events", []):
            print(f"  ⚡ CORR BREAK: {ev['note']}")
    except Exception as e:
        log.warning("Correlation breaks failed: %s", e)

    # Expensive per-ticker lookups (yfinance/news) only for plausible candidates:
    # congress-flagged, technically interesting, or an opportunity-scanner hit.
    congress_tickers_set = {s.ticker for s in herd_signals}
    interesting = [t for t, s in stocks.items()
                   if t in congress_tickers_set or s.rsi_14 <= 40 or s.rsi_14 >= 75
                   or abs(s.price_change_5d) >= 4 or t in opp_candidates][:12]

    options_flows, earn_radar = {}, {}
    try:
        from options_flow import get_flow
        options_flows = get_flow(interesting)
    except Exception as e:
        log.warning("Options flow failed: %s", e)
    try:
        from earnings_radar import get_radar
        earn_radar = get_radar(interesting)
    except Exception as e:
        log.warning("Earnings radar failed: %s", e)

    # Composite signal per ticker — deterministic, explainable
    from signal_aggregator import aggregate, save_snapshot
    signals = aggregate(stocks, SECTOR_MAP, herd_signals=herd_signals,
                        member_weights=member_weights, options_flows=options_flows,
                        earnings_radar=earn_radar, corr_breaks=corr_breaks)
    print(f"\nComposite signals (top 8):")
    ranked = sorted(signals.values(), key=lambda s: s.score, reverse=True)
    for s in ranked[:8]:
        print(f"  {s.ticker:6s} {s.score:+.3f} {s.conviction}")

    # Claude theses ONLY for names that cleared the composite bar (or congress herd).
    # PolyBench: LLM narrates and vetoes; the composite originates conviction.
    #
    # Sector headwind gate (added 2026-07-23, CC):
    # If a stock's sector is in active outflow (5d return negative) AND the stock
    # itself is bouncing (5d negative or RSI > 60 after a drawdown), the entry is
    # likely catching a falling knife. Demote to "none" — don't even send to the
    # LLM for thesis. This gate addresses the stop-out pattern where sector
    # rotation reversals hit within the first week.
    for s in ranked:
        if s.conviction != "none":
            sector_comp = next((c for c in s.components if c.name == "sector"), None)
            stock = stocks.get(s.ticker)
            if sector_comp and stock and sector_comp.score < 0:
                if stock.price_change_5d < 0 or stock.rsi_14 > 65:
                    log.info(
                        "Sector headwind gate: %s demoted (sector %s, 5d=%+.1f%%, RSI=%.1f)",
                        s.ticker, sector_comp.detail, stock.price_change_5d, stock.rsi_14,
                    )
                    s.risk_flags.append(
                        f"sector headwind gate: {sector_comp.detail}, "
                        f"stock 5d={stock.price_change_5d:+.1f}%"
                    )
                    s.conviction = "none"

    # Sector exhaustion gate (added 2026-07-23, CC):
    # If the sector is rallying (positive score) but the stock's RSI > 65, the
    # entry is likely catching the TOP of a sector rally — exactly the pattern
    # that caused most early losses. Don't kill the signal entirely (sector IS positive),
    # but demote conviction so the LLM sees it at lower size. This prevents
    # full-size entries into overbought names riding a sector wave that's about
    # to exhaust.
    for s in ranked:
        if s.conviction not in ("none",):
            sector_comp = next((c for c in s.components if c.name == "sector"), None)
            stock = stocks.get(s.ticker)
            if sector_comp and stock and sector_comp.score > 0 and stock.rsi_14 > 65:
                demote = {"high": "medium", "medium": "low", "low": "low"}
                old_conv = s.conviction
                s.conviction = demote.get(s.conviction, s.conviction)
                s.risk_flags.append(
                    f"sector exhaustion risk: {sector_comp.detail}, RSI={stock.rsi_14:.1f}"
                )
                log.info(
                    "Sector exhaustion gate: %s demoted %s→%s (sector %s score=%+.3f, RSI=%.1f)",
                    s.ticker, old_conv, s.conviction,
                    sector_comp.detail, sector_comp.score, stock.rsi_14,
                )

    # Snapshot AFTER the gates. It used to be saved before them, so every
    # demotion the gates logged ("demoted medium→low") existed only in
    # memory: execute read the pre-gate conviction and sized it at medium.
    snap_path = save_snapshot(signals, run_id=run_id)
    log.info("Signal snapshot (post-gate): %s", snap_path)

    thesis_candidates = [s for s in ranked
                         if s.conviction != "none" or s.ticker in congress_tickers_set][:8]
    theses = []
    for sig in thesis_candidates:
        ticker, stock = sig.ticker, stocks[sig.ticker]
        log.info("Analyzing %s (composite %+.3f)...", ticker, sig.score)
        congress_ctx = ""
        if herd_signals:
            try:
                congress_ctx = format_herd_for_thesis(herd_signals, ticker=ticker)
            except Exception:
                pass
        thesis = generate_thesis(ticker, stock, macro, sector_context=sector_summary,
                                  congress_context=congress_ctx,
                                  signal_context=sig.explain())
        if thesis is None:
            thesis = fallback_thesis(ticker, stock, sig)
        theses.append(thesis)

        marker = {"buy": "🟢", "sell": "🔴", "hold": "⚪"}.get(thesis.direction, "⚪")
        print(f"\n{marker} {ticker:6s} ${stock.current_price:>8.2f} | "
              f"RSI={stock.rsi_14:>5.1f} | 5d={stock.price_change_5d:>+5.1f}% | "
              f"composite={sig.score:+.3f}")
        print(f"  {thesis.direction.upper():4s} conviction={thesis.conviction} "
              f"size={thesis.position_size_pct:.0f}%")
        print(f"  {thesis.reasoning}")
        if thesis.risk_factors:
            print(f"  Risks: {', '.join(thesis.risk_factors)}")

    actionable = [t for t in theses if t.direction == "buy" and t.conviction in ("high", "medium", "low")]
    if actionable:
        print(f"\n{'='*60}")
        print(f"ACTIONABLE ({len(actionable)}):")
        for t in sorted(actionable, key=lambda x: {"high": 0, "medium": 1}.get(x.conviction, 2)):
            print(f"  {t.ticker}: {t.conviction.upper()} conviction, "
                  f"${t.entry_price:.2f} entry, ${t.stop_loss:.2f} stop, "
                  f"${t.target:.2f} target ({t.position_size_pct:.0f}%)")
    else:
        print(f"\nNo actionable opportunities. Patience is a position.")

    macro_out = vars(macro)
    macro_out.update({"regime_score": regime.get("score"),
                      "exposure_multiplier": regime.get("exposure_multiplier", 0.75),
                      "regime_components": regime.get("components", {})})
    results_file = RESULTS_DIR / f"scan_{run_id}.json"
    results_file.write_text(json.dumps(
        {"run_id": run_id, "signals_file": snap_path.name,
         "macro": macro_out, "sector_summary": sector_summary,
         "watchlist_size": len(watchlist), "theses": [vars(t) for t in theses]}, indent=2))
    log.info("Scan saved to %s", results_file)

    try:
        from sovereign_alerts import alert_high_conviction
        alert_high_conviction(signals, theses)
    except Exception as e:
        log.warning("Alerting failed: %s", e)

    return theses


def single_thesis(ticker: str):
    """Generate thesis for a single ticker."""
    macro = get_macro_context()
    stock = get_stock_data(ticker, market_open=macro.market_open)

    sig = None
    try:
        from signal_aggregator import aggregate
        from congress_scraper import load_existing_transactions, detect_herds
        from member_scoring import load_member_weights
        weights = load_member_weights()
        herds = detect_herds(load_existing_transactions(), member_weights=weights)
        sig = aggregate({ticker: stock}, SECTOR_MAP, herd_signals=herds,
                        member_weights=weights)[ticker]
    except Exception as e:
        log.warning("Composite signal failed for %s: %s", ticker, e)

    thesis = generate_thesis(ticker, stock, macro,
                             signal_context=sig.explain() if sig else "")
    if thesis is None and sig is not None:
        thesis = fallback_thesis(ticker, stock, sig)
    elif thesis is None:
        thesis = Thesis(ticker=ticker, direction="hold", conviction="none",
                        reasoning="LLM unavailable and no composite signal",
                        timestamp=datetime.now().isoformat())

    print(f"\n{'='*60}")
    print(f"THESIS: {ticker}")
    print(f"{'='*60}")
    print(f"Price: ${stock.current_price:.2f} | RSI: {stock.rsi_14:.1f}")
    print(f"5d: {stock.price_change_5d:+.1f}% | 30d: {stock.price_change_30d:+.1f}%")
    print(f"Macro: {macro.macro_regime} (VIX={macro.vix:.1f})")
    print(f"\nDirection: {thesis.direction.upper()}")
    print(f"Conviction: {thesis.conviction}")
    print(f"Size: {thesis.position_size_pct:.0f}% of portfolio")
    print(f"Entry: ${thesis.entry_price:.2f}")
    print(f"Stop: ${thesis.stop_loss:.2f}")
    print(f"Target: ${thesis.target:.2f}")
    print(f"\nReasoning: {thesis.reasoning}")
    if thesis.risk_factors:
        print(f"Risks: {', '.join(thesis.risk_factors)}")

    return thesis


def main():
    parser = argparse.ArgumentParser(description="Sovereign Pipeline — CC's market intelligence")
    parser.add_argument("command", choices=["scan", "portfolio", "thesis", "execute", "manage"],
                        help="Command to run")
    parser.add_argument("ticker", nargs="?", help="Ticker for thesis command")
    parser.add_argument("--watchlist", nargs="+", help="Custom watchlist for scan")
    args = parser.parse_args()

    if args.command == "portfolio":
        get_portfolio()
    elif args.command == "scan":
        scan_opportunities(args.watchlist)
    elif args.command == "thesis":
        if not args.ticker:
            print("Usage: sovereign_pipeline.py thesis TICKER")
            sys.exit(1)
        single_thesis(args.ticker.upper())
    elif args.command == "execute":
        from sovereign_execute import execute
        execute()
    elif args.command == "manage":
        from sovereign_execute import manage
        manage()


if __name__ == "__main__":
    main()
