# Track Record — Real Market Data, Real Decisions

These are not simulations. Every file in this directory contains actual decisions
made by the Sovereign Market Intelligence Agent against live market data,
executed on Alpaca paper trading.

## Files

### autonomous_run_001.json
First autonomous decision loop. March 27, 2026.
- **Market conditions:** CAUTIOUS regime, VIX 27.44, flat yield curve
- **Decision:** BUY 98 shares JNJ @ $242.09 (defensive healthcare play)
- **Reasoning:** Elevated volatility + restrictive Fed → rotate into defensive quality
- **Safety:** 8-layer validation passed, macro modifier reduced position size by 50%

### strategy_reviews.jsonl
Hourly strategy reviews (JSONL format, one review per line).
- Each entry contains: regime, risk score, position data, technical signals, and Claude's recommendation
- Demonstrates the agent adapting to regime changes in real time
- First regime shift (CAUTIOUS → NEUTRAL): agent recommended exiting JNJ

### How to Read

```python
import json

# Autonomous runs
with open("autonomous_run_001.json") as f:
    run = json.load(f)
    for decision in run["decisions"]:
        print(f"Phase: {decision['phase']}, Tools: {decision['tools_called']}")

# Strategy reviews
with open("strategy_reviews.jsonl") as f:
    for line in f:
        review = json.loads(line)
        print(f"{review['timestamp']}: {review['recommendation'][:100]}")
```

## What This Proves

1. **The agent makes real decisions** — not theoretical capabilities, actual trades
2. **Safety systems work under pressure** — macro overlay halved position sizes in CAUTIOUS regime
3. **The agent adapts** — regime shift from CAUTIOUS to NEUTRAL triggered thesis reevaluation
4. **Knowledge graph enrichment** — each run feeds back into institutional memory
5. **Structured audit trail** — every decision is logged, timestamped, and verifiable
