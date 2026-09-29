"""Performance metrics for bt_engine backtest results.

Sharpe, max drawdown, profit factor, and win rate broken down by signal
type — so a blended (composite) backtest can show which source actually
drove the returns instead of hiding it in one aggregate number.
"""

import math
from datetime import datetime, timedelta


def _daily_equity_series(equity_curve: list, start_equity: float) -> list:
    """Forward-fill the trade-resolution equity curve onto a daily grid.

    Equity only updates at trade exits, so Sharpe on the raw curve would
    understate volatility timing. Forward-filling to a daily grid is the
    standard cheap approximation when a full daily mark isn't available.
    """
    if not equity_curve:
        return []
    points = [(datetime.strptime(pt["date"], "%Y-%m-%d"), pt["equity"]) for pt in equity_curve]
    points.sort()
    series = []
    equity = start_equity
    d = points[0][0]
    idx = 0
    end = points[-1][0]
    while d <= end:
        while idx < len(points) and points[idx][0] <= d:
            equity = points[idx][1]
            idx += 1
        series.append(equity)
        d += timedelta(days=1)
    return series


def sharpe_ratio(equity_curve: list, start_equity: float, annualization: int = 252,
                 risk_free: float = 0.0) -> float:
    series = _daily_equity_series(equity_curve, start_equity)
    if len(series) < 2:
        return 0.0
    rets = [series[i] / series[i - 1] - 1 for i in range(1, len(series)) if series[i - 1] > 0]
    if not rets:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / len(rets)
    std = math.sqrt(var)
    if std == 0:
        return 0.0
    daily_sharpe = (mean - risk_free / annualization) / std
    return round(daily_sharpe * math.sqrt(annualization), 3)


def max_drawdown(equity_curve: list, start_equity: float) -> float:
    peak = start_equity
    max_dd = 0.0
    for pt in equity_curve:
        peak = max(peak, pt["equity"])
        if peak > 0:
            max_dd = max(max_dd, 1 - pt["equity"] / peak)
    return round(max_dd, 4)


def profit_factor(trades: list) -> float:
    gains = sum(t["return"] for t in trades if t["return"] > 0)
    losses = -sum(t["return"] for t in trades if t["return"] <= 0)
    if losses > 0:
        return round(gains / losses, 3)
    return round(gains, 3) if gains > 0 else 0.0


def win_rate_by_signal_type(trades: list) -> dict:
    """executed trade dicts (with 'signal_type' and 'return') → per-type stats."""
    by_type = {}
    for t in trades:
        by_type.setdefault(t["signal_type"], []).append(t)

    out = {}
    for stype, ts in by_type.items():
        rets = [t["return"] for t in ts]
        wins = [r for r in rets if r > 0]
        losses = [r for r in rets if r <= 0]
        out[stype] = {
            "trade_count": len(ts),
            "win_rate": round(len(wins) / len(rets), 3) if rets else 0.0,
            "avg_return": round(sum(rets) / len(rets), 4) if rets else 0.0,
            "avg_win": round(sum(wins) / len(wins), 4) if wins else 0.0,
            "avg_loss": round(sum(losses) / len(losses), 4) if losses else 0.0,
            "best": round(max(rets), 4) if rets else 0.0,
            "worst": round(min(rets), 4) if rets else 0.0,
        }
    return out


def summarize(result: dict) -> dict:
    """Full metrics bundle for a bt_engine.simulate() result."""
    if "error" in result:
        return result
    executed = result.get("executed", [])
    curve = result.get("equity_curve", [])
    start_equity = result.get("start_equity", 100.0)
    rets = [t["return"] for t in executed]
    wins = [r for r in rets if r > 0]

    return {
        "trades_taken": result.get("trades_taken", 0),
        "trades_skipped_full_book": result.get("trades_skipped_full_book", 0),
        "final_equity": result.get("final_equity"),
        "total_return": round(result.get("final_equity", start_equity) / start_equity - 1, 4),
        "win_rate": round(len(wins) / len(rets), 3) if rets else 0.0,
        "avg_win": round(sum(wins) / len(wins), 4) if wins else 0.0,
        "avg_loss": (round(sum(r for r in rets if r <= 0) / max(len(rets) - len(wins), 1), 4)
                    if rets else 0.0),
        "profit_factor": profit_factor(executed),
        "sharpe": sharpe_ratio(curve, start_equity),
        "max_drawdown": max_drawdown(curve, start_equity),
        "exits": {k: sum(1 for t in executed if t["exit_why"] == k)
                 for k in ("stop", "trail_stop", "target", "time")},
        "by_signal_type": win_rate_by_signal_type(executed),
        "equity_curve": curve,
    }
