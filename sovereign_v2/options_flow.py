"""Options flow analysis — unusual options activity as a leading indicator.

Real UOA feeds cost real money. This is the shoestring proxy via yfinance
option chains, which is still informative:

  - skew      = (call_vol - put_vol) / total_vol         in [-1, +1]
  - intensity = today's total option volume / open interest, vs the ~0.15
                typical churn. vol/OI >= 0.5 means someone is opening size.
  - score     = skew * min(intensity / 0.5, 1)

A big positive score = heavy, call-skewed opening flow (bullish tell).
Cached per-day per-ticker; yfinance is slow and rate-limited, so the
aggregator only asks about tickers already on the candidate list.

Usage:  python3 options_flow.py NVDA AMD ...
"""

import logging
import sys
from datetime import datetime

from sovereign_config import RESULTS_DIR, clamp, load_json, save_json

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("options_flow")

CACHE_FILE = RESULTS_DIR / "options_flow_cache.json"
UNUSUAL_VOL_OI = 0.5   # vol/OI at/above this = unusual opening flow
NEARBY_EXPIRIES = 2


def _analyze_ticker(sym: str) -> dict:
    import yfinance as yf

    t = yf.Ticker(sym)
    try:
        expiries = list(t.options)[:NEARBY_EXPIRIES]
    except Exception as e:
        return {"ticker": sym, "error": f"no chain: {e}"}
    if not expiries:
        return {"ticker": sym, "error": "no listed options"}

    call_vol = put_vol = call_oi = put_oi = 0
    for exp in expiries:
        try:
            chain = t.option_chain(exp)
        except Exception as e:
            log.warning("%s %s chain failed: %s", sym, exp, e)
            continue
        for df, is_call in ((chain.calls, True), (chain.puts, False)):
            vol = int(df["volume"].fillna(0).sum())
            oi = int(df["openInterest"].fillna(0).sum())
            if is_call:
                call_vol += vol
                call_oi += oi
            else:
                put_vol += vol
                put_oi += oi

    total_vol = call_vol + put_vol
    total_oi = call_oi + put_oi
    if total_vol < 100:  # illiquid chain, no signal
        return {"ticker": sym, "score": 0.0, "detail": "illiquid options", "unusual": False}

    skew = (call_vol - put_vol) / total_vol
    vol_oi = total_vol / max(total_oi, 1)
    intensity = min(vol_oi / UNUSUAL_VOL_OI, 1.0)
    score = clamp(skew * intensity)
    pc_ratio = put_vol / max(call_vol, 1)

    return {
        "ticker": sym,
        "score": round(score, 3),
        "unusual": vol_oi >= UNUSUAL_VOL_OI,
        "call_vol": call_vol,
        "put_vol": put_vol,
        "pc_ratio": round(pc_ratio, 2),
        "vol_oi": round(vol_oi, 3),
        "expiries": expiries,
        "detail": f"P/C {pc_ratio:.2f}, vol/OI {vol_oi:.2f}"
                  + (" ⚡UNUSUAL" if vol_oi >= UNUSUAL_VOL_OI else ""),
    }


def get_flow(tickers: list[str]) -> dict[str, dict]:
    """Per-ticker flow snapshot, cached for the calendar day."""
    today = datetime.now().strftime("%Y-%m-%d")
    cache = load_json(CACHE_FILE, {})
    if cache.get("date") != today:
        cache = {"date": today, "tickers": {}}

    out = {}
    for sym in tickers:
        if sym in cache["tickers"]:
            out[sym] = cache["tickers"][sym]
            continue
        log.info("Options flow: %s", sym)
        result = _analyze_ticker(sym)
        cache["tickers"][sym] = result
        out[sym] = result

    save_json(CACHE_FILE, cache)
    return out


if __name__ == "__main__":
    syms = [s.upper() for s in sys.argv[1:]] or ["NVDA", "AMD", "PLTR", "COIN"]
    flows = get_flow(syms)
    print(f"\nOPTIONS FLOW — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    for sym, f in sorted(flows.items(), key=lambda kv: abs(kv[1].get("score", 0)), reverse=True):
        if "error" in f:
            print(f"  {sym:6s} — {f['error']}")
        else:
            print(f"  {sym:6s} score={f.get('score', 0):+.2f}  {f.get('detail', '')}")
