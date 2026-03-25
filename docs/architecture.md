# Architecture

## System Overview

```
┌─────────────────────────────────────────────────────────────┐
│                    Decision Loop                             │
│         discover → plan → execute → verify                   │
│                                                              │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐   │
│  │ DISCOVER │→│   PLAN   │→│ EXECUTE  │→│  VERIFY  │   │
│  │ Scan     │  │ Analyze  │  │ Trade    │  │ Confirm  │   │
│  │ markets  │  │ deeply   │  │ safely   │  │ & store  │   │
│  └──────────┘  └──────────┘  └──────────┘  └──────────┘   │
└─────────────────────────────────────────────────────────────┘
        │                │              │              │
        ▼                ▼              ▼              ▼
┌──────────────┐ ┌──────────────┐ ┌──────────┐ ┌──────────────┐
│   Analysis   │ │   Analysis   │ │ Execution│ │ Integrations │
│              │ │              │ │          │ │              │
│ • Technical  │ │ • Macro      │ │ • Risk   │ │ • Storacha   │
│ • Sentiment  │ │ • Congress   │ │   Manager│ │   (IPFS)     │
│ • Portfolio  │ │ • Sector     │ │ • Order  │ │ • ERC-8004   │
│ • Scanner    │ │   Allocation │ │   Exec   │ │ • Lit Proto  │
└──────────────┘ └──────────────┘ └──────────┘ └──────────────┘
                                       │
                                       ▼
                                ┌──────────────┐
                                │    Safety    │
                                │              │
                                │ 8-Layer      │
                                │ Guardrails   │
                                │ + Anomaly    │
                                │ Detection    │
                                └──────────────┘
```

## Module Map

### Core (`core/`)
- **react_agent.py** — ReAct reasoning engine. Iterates Thought → Action → Observation until reaching a final answer. Produces structured execution logs.
- **decision_loop.py** — Autonomous 4-phase orchestrator. No human in the loop.
- **tool_registry.py** — Extensible tool system. Any function can be registered as an agent tool.
- **config.py** — Centralized configuration. All API keys, thresholds, and parameters.

### Analysis (`analysis/`)
- **technical.py** — SMA (20/50/200), RSI(14), MACD(12/26/9), Bollinger Bands. Generates composite signals.
- **sentiment.py** — News sentiment via Yahoo Finance RSS. Optional FinBERT for advanced NLP.
- **congressional.py** — Individual STOCK Act disclosure tracking via RapidAPI.
- **congressional_aggregate.py** — Cross-Congress pattern detection: sector trends, party divergence.
- **macro.py** — FRED API: yield curve, VIX, credit spreads, fed funds, unemployment. 5 risk regimes.
- **portfolio.py** — Correlation matrix, beta, Sharpe ratio, diversification scoring.
- **sector.py** — Sector exposure vs S&P 500 benchmark. Concentration risk flagging.

### Execution (`execution/`)
- **risk_manager.py** — 8-layer safety system. Position sizing, circuit breakers, macro overlay.
- **order_executor.py** — Dual-mode: paper trading (local) or live (Alpaca API).
- **portfolio_manager.py** — Position tracking, P&L, state persistence.
- **strategy.py** — Claude-powered strategic review for complex decisions.
- **scanner.py** — Multi-category technical screening for opportunity discovery.

### Safety (`safety/`)
- **guardrails.py** — Pre-trade validation pipeline with full audit trail.
- **anomaly_detector.py** — Price, volume, and portfolio drift anomaly detection.

### Integrations (`integrations/`)
- **erc8004/** — Agent identity and reputation on Base L2.
- **storacha/** — Immutable execution log storage on IPFS/Filecoin.
- **lit_protocol/** — Encrypted premium signals with token-gated access control.

## Data Flow

1. **Market data in**: Yahoo Finance (prices, financials, news), FRED (macro), RapidAPI (congressional)
2. **Analysis**: Each data source produces structured signals with confidence levels
3. **Synthesis**: ReAct agent combines signals through reasoning, not simple aggregation
4. **Safety gate**: 8-layer validation before any trade execution
5. **Execution**: Alpaca API for paper/live trading
6. **Audit**: Structured log → Storacha (IPFS) → ERC-8004 metadata (Base L2)
7. **Distribution**: Premium reports encrypted via Lit Protocol, free tier unencrypted

## Cost

~$0.05 per full analysis cycle (Claude API). Market data is free (Yahoo Finance, FRED). Congressional data has a 100 req/month free tier (RapidAPI).
