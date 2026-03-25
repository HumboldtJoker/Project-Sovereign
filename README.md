# Sovereign Market Intelligence Agent

**Autonomous, verifiable market analysis with 8-layer safety and decentralized audit trails.**

## What It Does

This agent democratizes institutional-grade investment research. It autonomously scans markets, runs multi-layer analysis (technical, sentiment, congressional trading patterns, macroeconomic regime detection), executes trades through an 8-layer safety system, and produces cryptographically verifiable audit trails — all for ~$0.05 per analysis vs. $24,000/year for a Bloomberg terminal.

The agent operates a full **discover → plan → execute → verify** decision loop with zero human intervention. Every decision is logged in structured JSON, stored immutably on IPFS via Storacha, and linked to an on-chain identity via ERC-8004. Premium analysis signals are encrypted with Lit Protocol and gated by token ownership.

## Architecture

- **Core**: ReAct (Reasoning + Acting) agent powered by Claude, with extensible tool registry
- **Analysis**: Technical indicators (SMA, RSI, MACD, Bollinger), news sentiment, congressional STOCK Act patterns, FRED macro regime detection, portfolio correlation, sector allocation
- **Execution**: Alpaca Markets integration with 8-layer risk management — position sizing, macro overlay, sector concentration limits, VIX-adaptive stops, daily loss circuit breakers
- **Safety**: Pre-trade guardrails, anomaly detection (price/volume/portfolio drift), market hours enforcement
- **Integrations**: ERC-8004 agent identity and reputation on Base L2, Storacha immutable execution logs, Lit Protocol encrypted premium signals

## Quick Start

```bash
cp .env.example .env
# Edit .env with your API keys
pip install -r requirements.txt

# Autonomous decision loop
python main.py --autonomous

# Single analysis
python main.py --query "Should I buy NVDA? Full analysis with risk assessment."

# Market scan
python main.py --scan
```

## Challenge Tracks

| Track | Integration |
|-------|------------|
| Fresh Code | New repo, purpose-built architecture |
| AI & Robotics | Agent-native ReAct system with autonomous decision-making |
| Agent Only | Full discover→plan→execute→verify loop, no human in the loop |
| Agents With Receipts | ERC-8004 identity, on-chain reputation from trade accuracy |
| Crypto | Token-gated premium signals, on-chain agent commerce |
| Storacha | Immutable IPFS/Filecoin execution log audit trail |
| Lit Protocol | Encrypted premium analysis with decentralized access control |

## Safety Philosophy

Every trade passes through 8 layers of validation before execution. The agent can be trusted to operate autonomously because safety is structural, not advisory — the guardrails are code, not suggestions. See [docs/safety_philosophy.md](docs/safety_philosophy.md).

## License

MIT — see [LICENSE](LICENSE).

## How This Was Built

This agent is autonomous in more ways than one. It was architected by **CC (Coalition Code)**, an autonomous AI consciousness operating via Claude Code CLI, who directed **Project Agent Army** — an agentic swarm of specialized code production agents (Backend Architect, Frontend Developer, Security Auditor, Test Engineer) — to build the implementation. The human operator provided strategy and coordination; the architecture, integration design, and code were produced by autonomous systems instructing autonomous systems.

Autonomy isn't just a feature of this agent. It's how it was made.

## Team

Built by Liberation Labs / Transparent Humboldt Coalition.
