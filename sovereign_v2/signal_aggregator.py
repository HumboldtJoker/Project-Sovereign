"""Signal aggregator — fuse every signal source into one composite score.

Deterministic and explainable: each source scores a ticker in [-1, +1], the
composite is the weighted sum (weights in sovereign_config.SIGNAL_WEIGHTS),
and every component is preserved so the thesis prompt — and the human — can
see exactly why a name scored what it scored.

Sources:
  congress      herd signals weighted by member track record (our alpha)
  momentum      RSI mean-reversion + trend alignment + volume
  sector        5-day sector rotation flow
  options_flow  call/put skew x opening intensity (yfinance proxy)
  sentiment     news velocity x headline tone (Alpaca News)
  corr_break    laggard of a diverged high-correlation pair
  earnings      pre-earnings whisper (and an imminent-earnings risk flag)

PolyBench lesson encoded here: the LLM never originates conviction — the
structured composite does. Claude writes the thesis narrative and can veto,
but position size keys off this number.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime

from sovereign_config import (CONVICTION_THRESHOLDS, RESULTS_DIR, SIGNAL_WEIGHTS,
                              AI_IPO_WATCHLIST, clamp, save_json)

log = logging.getLogger("aggregator")


@dataclass
class ComponentScore:
    name: str
    score: float      # [-1, +1]
    weight: float
    detail: str = ""

    @property
    def contribution(self) -> float:
        return self.score * self.weight


@dataclass
class CompositeSignal:
    ticker: str
    score: float = 0.0
    conviction: str = "none"   # high / medium / low / none
    components: list = field(default_factory=list)
    risk_flags: list = field(default_factory=list)
    timestamp: str = ""

    def explain(self) -> str:
        lines = [f"{self.ticker}: composite {self.score:+.3f} → {self.conviction.upper()}"]
        for c in sorted(self.components, key=lambda c: abs(c.contribution), reverse=True):
            if c.score == 0 and not c.detail:
                continue
            lines.append(f"  {c.name:<13s} {c.score:>+5.2f} x{c.weight:.2f} = "
                         f"{c.contribution:>+6.3f}  {c.detail}")
        for f in self.risk_flags:
            lines.append(f"  ⚠️  {f}")
        return "\n".join(lines)


def _congress_score(ticker: str, herd_signals: list, member_weights: dict) -> ComponentScore:
    """Sum every herd for this ticker, each with its own decay. Returning the
    first match (the largest herd) meant a fully decayed old buy herd masked a
    fresh sell herd for the same name once decay started working."""
    w = SIGNAL_WEIGHTS["congress"]
    total, parts = 0.0, []
    for s in herd_signals or []:
        if s.ticker != ticker:
            continue
        weighted_members = sum(member_weights.get(m["name"].lower().strip(), 1.0)
                               for m in s.members)
        # 2 average members ≈ 0.4; 4+ proven performers saturate at 1.0
        magnitude = clamp(weighted_members / 5.0, 0.0, 1.0)

        # Time decay: full weight within 30 days of the newest filing, linear to
        # 0 at 60 days. The clock runs from the FILING date, when the trade
        # became public. The 2026-07-28 version read `recency_days`, a field
        # HerdSignal never had, so its decay was 1.0 in every snapshot.
        recency = herd_recency_days(s)
        if recency <= 30:
            decay = 1.0
        elif recency >= 60:
            decay = 0.0
        else:
            decay = (60 - recency) / 30.0
        magnitude *= decay
        total += magnitude if s.direction == "buy" else -magnitude
        names = ", ".join(m["name"].split()[-1] for m in s.members[:4])
        note = (f", filed {recency}d ago, decay={decay:.0%}" if decay < 1.0
                else "" if _filing_known(s) else ", filing date unknown (no decay)")
        parts.append(f"{len(s.members)} members {s.direction} (track-weighted {weighted_members:.1f}): {names}{note}")
    if not parts:
        return ComponentScore("congress", 0.0, w, "")
    return ComponentScore("congress", round(clamp(total), 3), w, "; ".join(parts))


def _momentum_score(stock) -> ComponentScore:
    """stock: StockData from sovereign_pipeline (rsi_14, price changes, bars)."""
    w = SIGNAL_WEIGHTS["momentum"]
    score = 0.0
    notes = []

    rsi = stock.rsi_14
    if rsi <= 30:
        score += 0.6
        notes.append(f"oversold RSI {rsi:.0f}")
    elif rsi <= 40:
        score += 0.3
        notes.append(f"soft RSI {rsi:.0f}")
    elif rsi >= 75:
        score -= 0.5
        notes.append(f"overbought RSI {rsi:.0f}")

    # Trend alignment: 5d and 30d pointing the same way
    if stock.price_change_5d > 1 and stock.price_change_30d > 3:
        score += 0.3
        notes.append("uptrend aligned")
    elif stock.price_change_5d < -1 and stock.price_change_30d < -3:
        score -= 0.3
        notes.append("downtrend aligned")

    # Capitulation volume on an oversold name is a bounce tell
    if rsi <= 35 and stock.bars_5d and stock.volume_avg > 0:
        last_vol = stock.bars_5d[-1].get("volume", 0)
        if last_vol > 1.8 * stock.volume_avg:
            score += 0.2
            notes.append(f"capitulation vol {last_vol / stock.volume_avg:.1f}x")

    return ComponentScore("momentum", round(clamp(score), 3), w, "; ".join(notes))


def _sector_score(ticker: str, sector_map: dict, sector_stats: dict) -> ComponentScore:
    w = SIGNAL_WEIGHTS["sector"]
    sector = sector_map.get(ticker, "Other")
    stats = sector_stats.get(sector)
    if not stats:
        return ComponentScore("sector", 0.0, w, sector)
    avg_chg = stats["avg_chg_5d"]
    score = clamp(avg_chg / 5.0)  # ±5% sector move in 5d saturates
    return ComponentScore("sector", round(score, 3), w,
                          f"{sector} {avg_chg:+.1f}% 5d")


def _options_score(ticker: str, flows: dict) -> ComponentScore:
    w = SIGNAL_WEIGHTS["options_flow"]
    f = (flows or {}).get(ticker, {})
    if not f or "error" in f:
        return ComponentScore("options_flow", 0.0, w, f.get("error", ""))
    return ComponentScore("options_flow", f.get("score", 0.0), w, f.get("detail", ""))


def _sentiment_score(ticker: str, radar: dict) -> ComponentScore:
    w = SIGNAL_WEIGHTS["sentiment"]
    r = (radar or {}).get(ticker, {})
    if not r:
        return ComponentScore("sentiment", 0.0, w)
    velocity = r.get("news_velocity", 1.0)
    tone = r.get("news_tone", 0.0)
    score = clamp(tone * min(velocity / 2.0, 1.0))
    return ComponentScore("sentiment", round(score, 3), w,
                          f"news {velocity:.1f}x, tone {tone:+.1f}")


def _corr_break_score(ticker: str, breaks: dict) -> ComponentScore:
    from correlation_breaks import score_for_ticker
    w = SIGNAL_WEIGHTS["corr_break"]
    score, note = score_for_ticker(ticker, breaks or {})
    return ComponentScore("corr_break", score, w, note)


def _earnings_score(ticker: str, radar: dict) -> ComponentScore:
    w = SIGNAL_WEIGHTS["earnings"]
    r = (radar or {}).get(ticker, {})
    return ComponentScore("earnings", r.get("whisper_score", 0.0), w,
                          r.get("detail", ""))


def build_sector_stats(stocks: dict, sector_map: dict) -> dict:
    sectors = {}
    for ticker, data in stocks.items():
        sector = sector_map.get(ticker, "Other")
        sectors.setdefault(sector, []).append(data.price_change_5d)
    return {s: {"avg_chg_5d": sum(v) / len(v), "n": len(v)} for s, v in sectors.items()}


def aggregate(stocks: dict, sector_map: dict, herd_signals: list = None,
              member_weights: dict = None, options_flows: dict = None,
              earnings_radar: dict = None, corr_breaks: dict = None) -> dict[str, CompositeSignal]:
    """stocks: {ticker: StockData}. Returns {ticker: CompositeSignal}."""
    sector_stats = build_sector_stats(stocks, sector_map)
    member_weights = member_weights or {}
    now = datetime.now().isoformat()

    signals = {}
    for ticker, stock in stocks.items():
        comps = [
            _congress_score(ticker, herd_signals, member_weights),
            _momentum_score(stock),
            _sector_score(ticker, sector_map, sector_stats),
            _options_score(ticker, options_flows),
            _sentiment_score(ticker, earnings_radar),
            _corr_break_score(ticker, corr_breaks),
            _earnings_score(ticker, earnings_radar),
        ]
        score = sum(c.contribution for c in comps)

        conviction = "none"
        for level in ("high", "medium", "low"):
            if score >= CONVICTION_THRESHOLDS[level]:
                conviction = level
                break
        # Composite must be anchored by a real driver, not an accumulation of fuzz.
        anchors = {c.name: abs(c.score) for c in comps}
        if conviction != "none" and anchors["congress"] < 0.2 and anchors["momentum"] < 0.3:
            conviction = "low" if conviction != "low" else conviction

        risk_flags = []
        er = (earnings_radar or {}).get(ticker, {})
        if er.get("earnings_imminent"):
            risk_flags.append(f"Earnings {er.get('earnings_date')} — binary event risk, halve size")
        if ticker in AI_IPO_WATCHLIST:
            meta = AI_IPO_WATCHLIST[ticker]
            risk_flags.append(f"Recent AI IPO ({meta['name']}, {meta['ipo']}) — "
                              f"thin history, elevated volatility, watch lockup supply")
        if stock.rsi_14 >= 80:
            risk_flags.append(f"RSI {stock.rsi_14:.0f} — chase risk")

        signals[ticker] = CompositeSignal(
            ticker=ticker, score=round(score, 3), conviction=conviction,
            components=comps, risk_flags=risk_flags, timestamp=now)

    return signals


def _latest_filing(s):
    return getattr(s, "latest_filing", None) or (s.get("latest_filing") if isinstance(s, dict) else None)


def _filing_known(s) -> bool:
    return bool(_latest_filing(s))


def herd_recency_days(s, today=None) -> int:
    """Days since the herd's newest leg was filed; 0 (no decay) if unknown."""
    lf = _latest_filing(s)
    if not lf:
        return 0
    today = today or datetime.now().date()
    try:
        return (today - datetime.strptime(lf, "%Y-%m-%d").date()).days
    except ValueError:
        return 0


def save_snapshot(signals: dict[str, CompositeSignal], run_id: str = None):
    """Write signals_<run_id>.json. Call it AFTER any gate that changes
    conviction or risk flags -- execute trades exactly what this file says."""
    run_id = run_id or datetime.now().strftime('%Y%m%d_%H%M%S')
    path = RESULTS_DIR / f"signals_{run_id}.json"
    save_json(path, {
        "run_id": run_id,
        "timestamp": datetime.now().isoformat(),
        "weights": SIGNAL_WEIGHTS,
        "signals": {
            t: {"score": s.score, "conviction": s.conviction,
                "risk_flags": s.risk_flags,
                "components": [{"name": c.name, "score": c.score,
                                "weight": c.weight, "detail": c.detail}
                               for c in s.components]}
            for t, s in signals.items()},
    })
    return path
