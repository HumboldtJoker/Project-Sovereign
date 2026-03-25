# Demo Video Script (≤ 3 minutes)

## 0:00–0:20 — The Problem
"Institutional investors pay $24,000 a year for a Bloomberg terminal. They have teams of analysts running multi-factor analysis. Retail investors get... stock tips on Reddit."

"What if an autonomous AI agent could deliver institutional-grade analysis for five cents?"

## 0:20–0:50 — Architecture
"This is the Sovereign Market Intelligence Agent. It uses a ReAct reasoning loop — the same Thought-Action-Observation pattern used by research scientists — powered by Claude."

[Show architecture diagram]

"It pulls data from five real sources: Yahoo Finance for prices and fundamentals, FRED for macroeconomic regime detection, congressional STOCK Act disclosures for insider signals, news feeds for sentiment, and Alpaca Markets for trade execution."

"All of this runs through an 8-layer safety system. Not suggestions to the AI — structural guardrails in code."

## 0:50–1:30 — Live Demo
"Let's watch it work. I'm launching the autonomous decision loop."

[Terminal: `python main.py --autonomous`]

"Phase 1: Discover. The agent scans the market — checks VIX, yield curve, identifies candidates."

"Phase 2: Plan. Multi-layer analysis on each candidate. Technicals, sentiment, congressional patterns, macro overlay."

"Phase 3: Execute. Every trade passes through 8 safety layers. Position sizing is macro-adjusted. Sector concentration is checked. Circuit breakers are armed."

"Phase 4: Verify. Fills confirmed. Execution log generated."

## 1:30–2:00 — Sponsor Integrations
"Now here's where it gets interesting."

[Show agent_log.json]
"Every decision is captured in a structured execution log."

[Show Storacha upload]
"That log is uploaded to IPFS via Storacha. Content-addressed, immutable. Here's the CID."

[Show ERC-8004 registration]
"The agent has an on-chain identity via ERC-8004 on Base L2. The execution log CID is stored as metadata. Anyone can verify what the agent actually did."

[Show Lit Protocol encryption]
"Premium signals — the congressional patterns, the macro overlay — are encrypted with Lit Protocol. Only token holders can decrypt. Decentralized paywall, no middleman."

## 2:00–2:30 — Safety Deep Dive
"The safety system isn't cosmetic."

[Show safety report from guardrails]

"8 layers. Position limits, macro-aware sizing, sector concentration, circuit breakers, cash reserves, VIX-adaptive stops, anomaly detection, market hours enforcement."

"The agent can't reason its way around these. They're code, not prompts."

## 2:30–3:00 — Impact
"Bloomberg terminal: $24,000/year. This agent: $0.05 per analysis."

"Every decision is verifiable on-chain. Every trade passes through safety guardrails. Premium intelligence is token-gated, not paywalled."

"This is what autonomous finance looks like when safety is structural, intelligence is decentralized, and the audit trail is permanent."

"And one more thing. This agent wasn't just designed by AI — it was built by an autonomous agentic swarm called Project Agent Army, directed by another autonomous AI agent. Autonomous systems building autonomous systems. It's agents all the way down."

"Sovereign Market Intelligence Agent. Let the agent cook."
