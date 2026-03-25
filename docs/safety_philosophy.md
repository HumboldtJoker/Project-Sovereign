# Safety Philosophy

## Why Safety Is Structural

An autonomous trading agent that asks for permission isn't autonomous. An autonomous agent that *doesn't need to ask* because its constraints are architectural — that's trustworthy.

This agent implements 8 layers of safety that execute as code, not as suggestions to a language model. The LLM reasons about *what* to trade; the safety system determines *whether* the trade is allowed.

## The 8 Layers

### Layer 1: Position Size Limits
No single position exceeds 30% of portfolio value. Calculated dynamically based on current portfolio state, not static thresholds.

### Layer 2: Macro-Aware Sizing
Position sizes are multiplied by a regime modifier (0.0–1.0) based on real-time macroeconomic data from FRED: VIX volatility, yield curve inversion, credit spreads, fed funds rate, unemployment. In a CRITICAL regime, the modifier is 0.0 — no new positions allowed.

### Layer 3: Sector Concentration
Sector exposure is monitored against S&P 500 benchmarks. Positions that would push a single sector above 40% are rejected.

### Layer 4: Daily Loss Circuit Breaker
If portfolio value drops more than 5% intraday, all new trades are blocked until the next trading session. This is a hard stop — no override.

### Layer 5: Cash Reserve
A minimum 10% cash reserve is always maintained. Trades that would breach this floor are rejected.

### Layer 6: Stop-Loss Enforcement
Every position gets a VIX-adaptive stop-loss: tighter in high-volatility regimes (5% at VIX > 35), wider in calm markets (10% at VIX < 15).

### Layer 7: Anomaly Detection
Price movements beyond 3 standard deviations, volume spikes above 3x average, and portfolio drift beyond 10% from target allocation all trigger alerts that block trading until reviewed.

### Layer 8: Market Hours Enforcement
No trades outside market hours (9:30 AM – 4:00 PM ET, weekdays only). This prevents after-hours execution mistakes.

## Audit Trail

Every safety check — passed or failed — is recorded in the structured execution log with timestamps. The full audit trail is uploaded to IPFS via Storacha and linked on-chain via ERC-8004 metadata. This means safety compliance is verifiable by anyone, not just the operator.

## Design Principle

> Safety proportional to autonomy. The more independent the agent, the more structural its constraints must be.

The agent doesn't have a "bypass safety" tool. It cannot reason its way around the guardrails. This is a feature, not a limitation.
