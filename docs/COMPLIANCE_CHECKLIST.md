# PL Genesis Hackathon — Compliance Checklist

## General Submission Requirements

| Requirement | Status | Notes |
|-------------|--------|-------|
| Public GitHub repo | DONE | github.com/HumboldtJoker/PLGenesis-Market-Agent |
| MIT/Apache-2 license | DONE | MIT License |
| ≤3 min demo video (YouTube) | TODO | Shot list complete, need to generate + record |
| 250–500 word project summary | DONE | README.md (487 words) |
| Track selection | TODO | Select on DevSpot at submission |
| Team social handles | TODO | Need to add to submission form |
| ≥1 sponsor API/SDK integration | DONE | Storacha, Lit Protocol, ERC-8004 (three sponsors) |

## Track-Specific Requirements

### 1. Fresh Code — $50,000 (Top 10 × $5,000)

| Requirement | Status | Evidence |
|-------------|--------|----------|
| New repo created after Feb 10, 2026 | DONE | First commit: 2026-03-25 |
| Not a fork | DONE | Clean repo, purpose-built architecture |

### 2. AI & Robotics — $6,000

| Requirement | Status | Evidence |
|-------------|--------|----------|
| Agent-native system | DONE | ReAct loop with autonomous decision-making |
| Verifiable AI | DONE | Structured execution logs + Storacha audit trail |
| Agent identity | DONE | ERC-8004 integration |

### 3. Agent Only: Let the Agent Cook — $4,000

| Requirement | Status | Evidence |
|-------------|--------|----------|
| Fully autonomous (no human in loop) | DONE | DecisionLoop runs discover→plan→execute→verify |
| ERC-8004 Identity | DONE | integrations/erc8004/identity.py |
| agent.json manifest | DONE | agent.json in repo root |
| agent_log.json structured logs | DONE | demo/sample_agent_log.json (real run data) |
| Real tool use (multi-tool) | DONE | 12+ tools: FRED, yfinance, Alpaca, congressional, etc. |
| Safety & guardrails | DONE | 8-layer system, safety/ module |
| ERC-8004 Identity (bonus 5%) | DONE | Full registration + reputation pipeline |

### 4. Agents With Receipts — $4,004

| Requirement | Status | Evidence |
|-------------|--------|----------|
| ERC-8004 Identity | DONE | identity.py |
| Reputation system | DONE | reputation.py with trade→feedback pipeline |
| Onchain verifiability | NEEDS DEMO | Code written, need live tx on Base |
| Trust-based interactions | PARTIAL | Reputation query exists, no agent-to-agent selection yet |
| agent.json + agent_log.json | DONE | Both present |

### 5. Crypto — $6,000

| Requirement | Status | Evidence |
|-------------|--------|----------|
| Onchain economies | DONE | ERC-8004 agent commerce |
| Token-gated access | DONE | Lit Protocol encryption |
| Agent-native commerce | DONE | Analysis reports as encrypted assets |

### 6. Lit Protocol — prize TBD

| Requirement | Status | Evidence |
|-------------|--------|----------|
| Lit Protocol integration | DONE | encryption.py + access_control.py |
| Token-gated encryption | DONE | ERC-20/ERC-721 conditions |
| Free/premium tier | DONE | classify_report_tier() |
| Network: chipotle | DONE | Updated from Naga |

### 7. Storacha — prize TBD

| Requirement | Status | Evidence |
|-------------|--------|----------|
| Storacha integration | DONE | storage.py + retrieval.py |
| Content-addressed storage | DONE | CID-based upload/retrieve |
| Audit trail | DONE | Execution logs → IPFS |

## Open Items to Close Before Submission

### CRITICAL (must do)
- [ ] Record demo video (≤3 min) and upload to YouTube
- [ ] Generate AI clips via ComfyUI on Beast
- [ ] Record screen captures (dashboard + terminal)
- [ ] Submit on DevSpot before March 31

### HIGH (strongly recommended)
- [ ] Live ERC-8004 registration on Base mainnet (need ~$0.50 ETH for gas)
- [ ] Live Storacha upload (need `storacha login` on this machine or Beast)
- [ ] One more autonomous run to show portfolio evolution
- [ ] Add trust-based interaction demo for Agents With Receipts track

### NICE TO HAVE
- [ ] Multiple agent_log.json samples showing different market conditions
- [ ] Backtest results showing historical performance
- [ ] Multi-agent swarm mode (encouraged by Agent Only track)

## Contract Addresses (Verified)

### Base Mainnet
- Identity Registry: `0x8004A169FB4a3325136EB29fA0ceB6D2e539a432`
- Reputation Registry: `0x8004BAa17C55a88189AE136b182e5fdA19dE9b63`

### Base Sepolia (testnet fallback)
- Identity Registry: `0x8004A818BFB912233c491871b3d84c89A494BD9e`
- Reputation Registry: `0x8004B663056A597Dffe9eCcC1965A193B7388713`

## Gas Costs (Base L2)
- Agent registration: ~$0.01-0.05
- Reputation feedback: ~$0.01
- Metadata update: ~$0.01
- Total for full demo: ~$0.50

Wallet: `0x59660cC429722cd84AFd46ce923d9fCA6988B589`
Needs: Small amount of ETH on Base (bridge from mainnet or buy directly)
