"""Sovereign backtest comparison report.

Runs three strategies through the shared bt_engine and lines them up against
a SPY buy-and-hold benchmark:

  congress   herd events only (>=2 members buying the same ticker within a
             30-day window, filing-date honest — same rule as live detection)
  momentum   RSI(14)<=30 mean-reversion entries on the core watchlist
  blended    both event streams fed into ONE shared book (max 6 concurrent,
             conviction-sized) — what the $100 account actually experiences
             day to day, since live scans surface both signal types together

Each strategy gets full metrics (Sharpe, max drawdown, profit factor) and a
win-rate-by-signal-type breakdown, so "blended" shows which source is
actually carrying the returns instead of hiding it behind one number.

Usage:
    python3 bt_report.py                # run + print + save JSON + HTML
    python3 bt_report.py --no-html      # skip the HTML report
"""

import argparse
import html
import json
import logging
import sys
from collections import defaultdict
from datetime import datetime, timedelta

import bt_metrics
from bt_data import closes, get_bars
from bt_engine import SignalEvent, simulate
from sovereign_config import DATA_DIR, RESULTS_DIR, load_json, save_json

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("bt_report")

HERD_WINDOW_DAYS = 30
HERD_MIN_MEMBERS = 2
CORE_WATCHLIST = ["AAPL", "GOOGL", "META", "AMZN", "NVDA", "MSFT",
                  "GEV", "PLTR", "COIN", "AMD", "TSM", "AVGO"]


# --------------------------------------------------------------------------
# Signal event builders
# --------------------------------------------------------------------------

def _parse_filing_date(s: str):
    try:
        d = datetime.strptime(s, "%m/%d/%Y")
        return d.date() if datetime(2022, 1, 1).date() <= d.date() <= datetime.now().date() else None
    except (ValueError, TypeError):
        return None


def _congress_conviction(weighted_members: float) -> str:
    magnitude = min(weighted_members / 5.0, 1.0)
    if magnitude >= 0.6:
        return "high"
    if magnitude >= 0.35:
        return "medium"
    return "low"


def congress_events() -> list:
    """Replay filings chronologically; emit herd events as they'd have appeared live."""
    txs = load_json(DATA_DIR / "transactions.json", [])
    try:
        from member_scoring import load_member_weights
        member_weights = load_member_weights() or {}
    except Exception:
        member_weights = {}

    buys = []
    for tx in txs:
        if tx.get("direction") != "buy" or not tx.get("ticker"):
            continue
        fd = _parse_filing_date(tx.get("filing_date", ""))
        if fd is None:
            continue
        buys.append({"ticker": tx["ticker"], "member": tx["member"].lower().strip(), "filed": fd})
    buys.sort(key=lambda b: b["filed"])

    window = defaultdict(list)
    events = []
    for b in buys:
        t, d = b["ticker"], b["filed"]
        window[t] = [(m, fd) for m, fd in window[t] if (d - fd).days <= HERD_WINDOW_DAYS]
        window[t].append((b["member"], d))
        members = {m for m, _ in window[t]}
        if len(members) >= HERD_MIN_MEMBERS:
            weighted = sum(member_weights.get(m, 1.0) for m in members)
            events.append(SignalEvent(
                ticker=t, date=d, signal_type="congress",
                conviction=_congress_conviction(weighted),
                meta={"members": sorted(members), "weighted_members": round(weighted, 2)}))
    return events


def _rsi(closes_only: list, period: int = 14) -> float:
    if len(closes_only) < period + 1:
        return 50.0
    deltas = [closes_only[i] - closes_only[i - 1] for i in range(1, len(closes_only))]
    gains = sum(d for d in deltas[-period:] if d > 0) / period
    losses = sum(-d for d in deltas[-period:] if d < 0) / period
    return 100.0 if losses == 0 else 100 - 100 / (1 + gains / losses)


def _momentum_conviction(rsi: float) -> str:
    if rsi <= 20:
        return "high"
    if rsi <= 25:
        return "medium"
    return "low"


def momentum_events(prices: dict, cooldown_days: int = 45) -> list:
    events = []
    for sym, series in prices.items():
        if sym == "SPY" or not series:
            continue
        closes_only = [c for _, c in series]
        last_signal = None
        for i in range(20, len(series)):
            rsi = _rsi(closes_only[max(0, i - 14):i + 1])
            if rsi <= 30:
                d = series[i][0]
                if last_signal and (d - last_signal).days < cooldown_days:
                    continue
                last_signal = d
                events.append(SignalEvent(ticker=sym, date=d, signal_type="momentum",
                                          conviction=_momentum_conviction(rsi),
                                          meta={"rsi": round(rsi, 1)}))
    return events


# --------------------------------------------------------------------------
# Benchmark
# --------------------------------------------------------------------------

def spy_pct_curve(spy_series: list, start_date, end_date) -> list:
    """SPY buy-and-hold, indexed to 0% at start_date, as a daily pct-return curve."""
    pts = [(d, c) for d, c in spy_series if start_date <= d <= end_date]
    if not pts:
        return []
    base = pts[0][1]
    return [{"date": str(d), "pct": round((c / base - 1) * 100, 2)} for d, c in pts]


def to_pct_curve(equity_curve: list, start_equity: float) -> list:
    return [{"date": pt["date"], "pct": round((pt["equity"] / start_equity - 1) * 100, 2)}
            for pt in equity_curve]


# --------------------------------------------------------------------------
# Run
# --------------------------------------------------------------------------

def run_all() -> dict:
    cong_events = congress_events()

    tickers = set(CORE_WATCHLIST) | {e.ticker for e in cong_events} | {"SPY"}
    earliest = (min(e.date for e in cong_events) if cong_events
               else (datetime.now() - timedelta(days=540)).date()) - timedelta(days=5)
    earliest = min(earliest, (datetime.now() - timedelta(days=540)).date())
    start_dt = datetime.combine(earliest, datetime.min.time())

    bars = get_bars(list(tickers), start_dt)
    prices = {sym: closes(b) for sym, b in bars.items()}
    spy_series = prices.get("SPY", [])

    mom_events = momentum_events({s: prices[s] for s in CORE_WATCHLIST if s in prices})

    strategies = {
        "congress": cong_events,
        "momentum": mom_events,
        "blended": cong_events + mom_events,
    }

    out = {"run_at": datetime.now().isoformat(), "strategies": {}}
    for name, events in strategies.items():
        result = simulate(events, prices)
        metrics = bt_metrics.summarize(result)
        if "error" in metrics:
            out["strategies"][name] = metrics
            continue

        executed = result["executed"]
        span_start = min(datetime.strptime(t["entry_date"], "%Y-%m-%d").date() for t in executed)
        span_end = max(datetime.strptime(t["exit_date"], "%Y-%m-%d").date() for t in executed)
        spy_curve = spy_pct_curve(spy_series, span_start, span_end)
        spy_total_return = round(spy_curve[-1]["pct"] / 100, 4) if spy_curve else None

        metrics["strategy_curve"] = to_pct_curve(metrics.pop("equity_curve"), result["start_equity"])
        metrics["spy_curve"] = spy_curve
        metrics["spy_return_same_span"] = spy_total_return
        metrics["edge_vs_spy"] = (round(metrics["total_return"] - spy_total_return, 4)
                                  if spy_total_return is not None else None)
        metrics["span"] = {"start": str(span_start), "end": str(span_end)}
        out["strategies"][name] = metrics

    return out


# --------------------------------------------------------------------------
# Console report
# --------------------------------------------------------------------------

def print_report(out: dict):
    print(f"\n{'=' * 78}")
    print("SOVEREIGN BACKTEST COMPARISON — filing-date-honest, live exit rules")
    print(f"{'=' * 78}")
    print(f"{'Strategy':<10} {'Return':>9} {'SPY span':>9} {'Edge':>8} "
          f"{'Sharpe':>7} {'MaxDD':>7} {'WinRate':>8} {'Trades':>7}")
    print("-" * 78)
    for name, m in out["strategies"].items():
        if "error" in m:
            print(f"{name:<10} {'—':>9}  ({m['error']})")
            continue
        spy = f"{m['spy_return_same_span']:+.1%}" if m["spy_return_same_span"] is not None else "n/a"
        edge = f"{m['edge_vs_spy']:+.1%}" if m["edge_vs_spy"] is not None else "n/a"
        print(f"{name:<10} {m['total_return']:>+8.1%} {spy:>9} {edge:>8} "
              f"{m['sharpe']:>7.2f} {m['max_drawdown']:>6.1%} "
              f"{m['win_rate']:>7.0%} {m['trades_taken']:>7}")

    blended = out["strategies"].get("blended", {})
    if "by_signal_type" in blended:
        print(f"\nBlended book — win rate by signal type:")
        for stype, s in blended["by_signal_type"].items():
            print(f"  {stype:<10} {s['trade_count']:>3} trades  win {s['win_rate']:>5.0%}  "
                  f"avg {s['avg_return']:>+6.1%}  best {s['best']:>+6.1%}  worst {s['worst']:>+6.1%}")


# --------------------------------------------------------------------------
# HTML comparison report
# --------------------------------------------------------------------------

CSS = """
:root{--surface:#fcfcfb;--page:#f9f9f7;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;
--grid:#e1e0d9;--axis:#c3c2b7;--border:rgba(11,11,11,.10);
--s-blended:#2a78d6;--s-congress:#1baf7a;--s-momentum:#eda100;--s-spy:#898781;
--good:#006300;--bad:#d03b3b}
@media(prefers-color-scheme:dark){:root{--surface:#1a1a19;--page:#0d0d0d;--ink:#fff;
--ink2:#c3c2b7;--grid:#2c2c2a;--axis:#383835;--border:rgba(255,255,255,.10);
--s-blended:#3987e5;--s-congress:#199e70;--s-momentum:#c98500;--s-spy:#898781;
--good:#0ca30c}}
*{box-sizing:border-box;margin:0}
body{font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;background:var(--page);
color:var(--ink);padding:24px;max-width:1080px;margin:0 auto}
h1{font-size:20px;margin-bottom:2px} h2{font-size:14px;color:var(--ink2);margin:28px 0 10px}
.sub{color:var(--muted);font-size:12px;margin-bottom:20px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px;overflow-x:auto;margin-bottom:16px}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;
text-align:right;padding:6px 10px;border-bottom:1px solid var(--grid)}
th:first-child,td:first-child{text-align:left}
td{padding:6px 10px;text-align:right;border-bottom:1px solid var(--grid);font-size:13px}
tr:last-child td{border-bottom:none}
.up{color:var(--good)} .down{color:var(--bad)}
.legend{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:10px;font-size:12px;color:var(--ink2)}
.legend span{display:inline-flex;align-items:center;gap:6px}
.swatch{width:14px;height:3px;border-radius:2px;display:inline-block}
svg text{fill:var(--muted);font:11px system-ui,sans-serif}
.note{color:var(--muted);font-size:12px;margin-top:8px}
"""

SERIES_COLORS = {"blended": "var(--s-blended)", "congress": "var(--s-congress)",
                 "momentum": "var(--s-momentum)", "spy": "var(--s-spy)"}
SERIES_LABELS = {"blended": "Blended (composite book)", "congress": "Congress herd",
                 "momentum": "Momentum (RSI≤30)", "spy": "SPY buy & hold"}


def _fmt_pct(x, none="n/a"):
    return f"{x:+.1%}" if x is not None else none


def _cls(x):
    return "up" if x and x > 0 else "down" if x and x < 0 else ""


def _comparison_svg(curves: dict, width=1000, height=280) -> str:
    """Overlay multiple pct-return curves (different start dates OK) on one axis."""
    all_dates = sorted({p["date"] for c in curves.values() for p in c})
    if len(all_dates) < 2:
        return "<p class='sub'>Not enough trade history yet to chart.</p>"
    date_x = {d: i for i, d in enumerate(all_dates)}
    n = len(all_dates) - 1

    all_pcts = [p["pct"] for c in curves.values() for p in c]
    lo, hi = min(all_pcts + [0]), max(all_pcts + [0])
    span = (hi - lo) or 10
    lo -= span * 0.08
    hi += span * 0.08

    pad_l, pad_r, pad_t, pad_b = 50, 90, 12, 24
    iw, ih = width - pad_l - pad_r, height - pad_t - pad_b

    def x(d): return pad_l + iw * date_x[d] / n
    def y(v): return pad_t + ih * (1 - (v - lo) / (hi - lo))

    grid, labels = [], []
    for frac in (0, .5, 1):
        v = lo + (hi - lo) * frac
        yy = y(v)
        grid.append(f'<line x1="{pad_l}" y1="{yy:.1f}" x2="{width - pad_r}" y2="{yy:.1f}" '
                    f'stroke="var(--grid)" stroke-width="1"/>')
        labels.append(f'<text x="{pad_l - 8}" y="{yy + 4:.1f}" text-anchor="end">{v:+.0f}%</text>')
    zero_y = y(0)
    grid.append(f'<line x1="{pad_l}" y1="{zero_y:.1f}" x2="{width - pad_r}" y2="{zero_y:.1f}" '
                f'stroke="var(--axis)" stroke-width="1"/>')
    for i in (0, len(all_dates) // 2, len(all_dates) - 1):
        labels.append(f'<text x="{x(all_dates[i]):.1f}" y="{height - 6}" text-anchor="middle">'
                      f'{all_dates[i]}</text>')

    lines = []
    for key, curve in curves.items():
        if not curve:
            continue
        pts = " ".join(f"{x(p['date']):.1f},{y(p['pct']):.1f}" for p in curve)
        dash = ' stroke-dasharray="5 4"' if key == "spy" else ""
        lines.append(f'<polyline points="{pts}" fill="none" stroke="{SERIES_COLORS[key]}" '
                     f'stroke-width="2"{dash} stroke-linejoin="round" stroke-linecap="round"/>')
        last = curve[-1]
        lines.append(f'<text x="{x(last["date"]) + 6:.1f}" y="{y(last["pct"]) + 4:.1f}" '
                     f'style="fill:{SERIES_COLORS[key]};font-weight:600">'
                     f'{SERIES_LABELS[key].split(" ")[0]} {last["pct"]:+.0f}%</text>')

    legend = "".join(
        f'<span><span class="swatch" style="background:{SERIES_COLORS[k]}'
        f'{";border-top:2px dashed var(--s-spy);height:0" if k == "spy" else ""}"></span>{SERIES_LABELS[k]}</span>'
        for k in curves if curves[k])

    return f"""<div class="legend">{legend}</div>
<svg viewBox="0 0 {width} {height}" width="100%" role="img" aria-label="Strategy comparison, return since inception">
  {''.join(grid)}
  {''.join(lines)}
  {''.join(labels)}
</svg>"""


def render_html(out: dict) -> str:
    strategies = out["strategies"]
    curves = {name: m.get("strategy_curve", []) for name, m in strategies.items() if "error" not in m}
    spy_curves = [m.get("spy_curve") for m in strategies.values() if "error" not in m and m.get("spy_curve")]
    if spy_curves:
        curves["spy"] = max(spy_curves, key=len)

    rows = ""
    for name, m in strategies.items():
        if "error" in m:
            rows += f"<tr><td>{name}</td><td colspan=7>{m['error']}</td></tr>"
            continue
        rows += (f"<tr><td>{SERIES_LABELS.get(name, name)}</td>"
                f"<td class='{_cls(m['total_return'])}'>{_fmt_pct(m['total_return'])}</td>"
                f"<td>{_fmt_pct(m['spy_return_same_span'])}</td>"
                f"<td class='{_cls(m['edge_vs_spy'])}'>{_fmt_pct(m['edge_vs_spy'])}</td>"
                f"<td>{m['sharpe']:.2f}</td><td>{m['max_drawdown']:.1%}</td>"
                f"<td>{m['win_rate']:.0%}</td><td>{m['trades_taken']}</td></tr>")

    blended = strategies.get("blended", {})
    bytype_rows = "".join(
        f"<tr><td>{stype}</td><td>{s['trade_count']}</td><td>{s['win_rate']:.0%}</td>"
        f"<td class='{_cls(s['avg_return'])}'>{s['avg_return']:+.1%}</td>"
        f"<td class='up'>{s['best']:+.1%}</td><td class='down'>{s['worst']:+.1%}</td></tr>"
        for stype, s in blended.get("by_signal_type", {}).items()
    ) or "<tr><td colspan=6>No blended trades yet</td></tr>"

    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sovereign Backtest Comparison</title><style>{CSS}</style></head><body>
<h1>Sovereign — Backtest Comparison</h1>
<p class="sub">Generated {out['run_at'][:16].replace('T', ' ')} · filing-date-honest congress
entries · live exit rules (-8% stop / +15% target / breakeven trail / 30-day max hold)</p>
<h2>Return since inception (each strategy starts at its first signal)</h2>
<div class="card">{_comparison_svg(curves)}</div>
<h2>Strategy comparison</h2>
<div class="card"><table>
<tr><th>Strategy</th><th>Return</th><th>SPY same span</th><th>Edge</th><th>Sharpe</th>
<th>Max DD</th><th>Win rate</th><th>Trades</th></tr>
{rows}</table></div>
<h2>Blended book — win rate by signal type</h2>
<div class="card"><table>
<tr><th>Signal type</th><th>Trades</th><th>Win rate</th><th>Avg return</th><th>Best</th><th>Worst</th></tr>
{bytype_rows}</table>
<p class="note">"Blended" replays congress and momentum signals through one shared $100 book
(max 6 concurrent, conviction-sized) — this is what the live account actually experiences,
since scans surface both signal types together. This breakdown shows which source is
actually carrying the blended return.</p></div>
</body></html>"""


def main():
    parser = argparse.ArgumentParser(description="Sovereign backtest comparison report")
    parser.add_argument("--no-html", action="store_true", help="skip HTML report")
    args = parser.parse_args()

    out = run_all()
    print_report(out)

    json_path = RESULTS_DIR / f"backtest_comparison_{datetime.now().strftime('%Y%m%d')}.json"
    save_json(json_path, out)
    print(f"\nSaved: {json_path}")

    if not args.no_html:
        html_path = RESULTS_DIR / "backtest_comparison.html"
        html_path.write_text(render_html(out))
        print(f"Saved: {html_path}")


if __name__ == "__main__":
    main()
