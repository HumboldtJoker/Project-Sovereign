# PL Genesis: Market-Analysis-Agent — Multi-Challenge Battle Plan

**Agent:** Code Production Specialist (Agent Army)
**Objective:** Rearchitect Market-Analysis-Agent (AutoInvestor) into a new Fresh Code repo that maximizes prize eligibility across PL Genesis hackathon challenges.
**Date:** March 24, 2026

---

## Hackathon Overview

- **Name:** PL_Genesis: Frontiers of Collaboration Hackathon
- **Platform:** [DevSpot](https://pl-genesis-frontiers-of-collaboration-hackathon.devspot.app/)
- **Total Prize Pool:** $155,500
- **Submissions Due:** March 31, 2026
- **Format:** Virtual
- **Team Max:** 5 individuals
- **Rules Doc:** [Google Doc](https://docs.google.com/document/d/1Hu2JNfujYAWTl5HyMKsIf4Sjt9u8qrTfJepuQ9zA65A)
- **Thomas is registered** (profile: Thomas Edrington)

### Submission Requirements

- Public GitHub repo (MIT, Apache-2, or similar license)
- ≤3 min demo video on YouTube
- 250–500 word project summary
- Track selection (Fresh Code or Existing Code)
- Team social handles
- Must integrate **at least one** sponsor API/SDK

### Judging Criteria (equal weight unless otherwise noted)

- Technical Excellence
- Integration Depth
- Utility & Impact
- Innovation
- Presentation & Documentation

### Sponsor APIs/SDKs (must use ≥1)

| Sponsor | Domain | Docs |
|---------|--------|------|
| Filecoin | Decentralized storage | TBD — research needed |
| Storacha | Decentralized storage (IPFS/Filecoin layer) | TBD |
| Lit Protocol | Decentralized access control, encryption | TBD |
| NEAR | L1 blockchain, account abstraction | TBD |
| Starknet | ZK-rollup, verifiable computation | TBD |
| Flow | L1 blockchain (consumer apps) | TBD |
| Impulse AI | Autonomous ML | TBD |
| Physical AI | Robotics/physical world | TBD |

---

## Source Codebase: Market-Analysis-Agent (AutoInvestor)

**Repo:** `github.com/HumboldtJoker/Market-Analysis-Agent`
**Commits:** 62 (production-grade)
**Language:** Python (primary), Node.js (MCP server)
**Agent Framework:** ReAct (Reasoning + Acting) via Claude Sonnet 4.5

### Core Modules

| Module | Purpose |
|--------|---------|
| `autoinvestor_api.py` | Unified API interface |
| `autoinvestor_react.py` | ReAct agent implementation |
| `trading_agent.py` | Primary trading orchestration |
| `collaborative_agent.py` | Human-AI collaborative mode |
| `technical_indicators.py` | SMA, RSI, MACD, Bollinger Bands |
| `news_sentiment.py` | Financial sentiment analysis |
| `congressional_trades.py` | Individual STOCK Act disclosures |
| `congressional_trades_aggregate.py` | Aggregate pattern analysis |
| `macro_agent.py` | Market regime detection (FRED, VIX, yield curve) |
| `portfolio_correlation.py` | Diversification scoring |
| `sector_allocation.py` | Concentration risk analysis |
| `risk_manager.py` | 8-layer safety system with macro overlay |
| `portfolio_manager.py` | Position tracking |
| `order_executor.py` | Trade placement (Alpaca) |
| `strategy_trigger.py` | Entry/exit logic |
| `scan_ai_ecosystem.py` | Multi-ticker screening |
| `overnight_scanner.py` | Post-market analysis |
| `sec_filings_rag.py` | RAG for 10-K/10-Q analysis |
| `mcp_server.py` / `mcp_server.js` | MCP protocol servers |
| `backtesting.py` | Historical strategy testing |

### Current API Integrations

| Service | Purpose |
|---------|---------|
| Alpaca Markets | Trade execution, portfolio mgmt |
| Yahoo Finance | Real-time pricing, financials, analyst ratings |
| FRED (St. Louis Fed) | Yield curve, VIX, credit spreads, unemployment |
| RapidAPI | Congressional trading data (STOCK Act) |
| Anthropic Claude | Sonnet 4.5 ReAct reasoning |

### Architecture Patterns

- **Decision Loop:** discover → analyze → plan → execute → verify (already maps to Agent Only requirements)
- **Safety:** 8-layer risk management, macro-aware position sizing, sector concentration limits, beta normalization
- **Modes:** Paper trading, live trading, local sim, collaborative (human-AI dialogue)
- **Cost:** ~$0.05/analysis

---

## In-House Tooling (NOT for submission)

### Project-Agent-Army / marketplace-skills
Specialist agents for code production: Backend Architect, Frontend Developer, Security Auditor, Test Engineer, Database Architect. Use these to BUILD the submission, not as part of it.

### Liberation Labs Coalition Resources
- **Curiosity Engine** — Autonomous research pipeline (queue-based, multi-database)
- **Project Emet** — Autonomous investigation agent (blockchain analysis, entity search, OSINT)
- **Agent-Memory-Architectures (Kintsugi-CMA)** — BDI governance + hybrid memory retrieval
- **research-mcp** — Unified academic literature MCP (OpenAlex + PubMed)
- **social-media-mcp** — Social media MCP with brand voice
- **MindPrint** — Proof of mind / verifiable AI compute

Patterns from these can be adapted but the submission repo must be new code.

---

## Target Challenges & Integration Map

### PRIMARY TARGETS

#### 1. Fresh Code — $50,000 (Top 10 × $5,000)
**Requirement:** Brand new public repo created after Feb 10, 2026 kickoff.
**Strategy:** Create new repo. Port and rearchitect AutoInvestor core with sponsor integrations baked in from day one. This is NOT a fork — it's a purpose-built submission.
**Status:** Eligible if new repo.

#### 2. AI & Robotics — $6,000 (1st $3K, 2nd $2K, 3rd $1K)
**Track description:** Agent-native systems, verifiable AI, machine coordination, agent identity and payments.
**Fit:** AutoInvestor IS an agent-native system. The ReAct loop with multi-tool orchestration, safety guardrails, and autonomous decision-making is exactly what this track wants.
**Integration needed:** None beyond what we're already building.

#### 3. Agent Only: Let the agent cook — $4,000 (1st $2K, 2nd $1.5K, 3rd $500)
**Sponsor:** Ethereum Foundation
**Shared track:** Synthesis Hackathon × PL_Genesis ($8K total across both)
**Core requirement:** Fully autonomous agent, NO human in the loop.

**Required components:**
1. **Autonomous Execution** — full decision loop: discover → plan → execute → verify → submit
2. **ERC-8004 Identity** — onchain agent identity linked to operator wallet
3. **Agent Capability Manifest** — `agent.json` with: agent name, operator wallet, ERC-8004 identity, supported tools, tech stacks, compute constraints, task categories
4. **Structured Execution Logs** — `agent_log.json` showing: decisions, tool calls, retries, failures, final outputs
5. **Real Tool Use** — must interact with actual tools/APIs. Multi-tool > single-tool.
6. **Safety & Guardrails** — safeguards before irreversible actions

**Judging weights:**
- Autonomy: 35%
- Tool Use: 25%
- Guardrails & Safety: 20%
- Impact: 15%
- ERC-8004 Integration: Bonus 5%

**Multi-agent swarms** with specialized roles (planner, developer, QA, deployer) encouraged.

**Fit:** AutoInvestor's ReAct loop already does discover → plan → execute → verify. Need to add: ERC-8004 identity, agent.json, agent_log.json, and remove all human-in-the-loop touchpoints for this submission path.

#### 4. Agents With Receipts — 8004 — $4,004 (prizes TBD)
**Sponsor:** Ethereum Foundation
**Shared track:** Synthesis × PL_Genesis
**Core requirement:** ERC-8004 integration for agent identity, reputation, verifiable onchain transactions.

**Required components:**
1. **ERC-8004 Identity** — registered agent identity linked to operator wallet
2. **Reputation System** — build/update reputation through task completion
3. **Onchain Verifiability** — viewable transactions on blockchain explorer
4. **Trust-Based Interactions** — selecting collaborators based on reputation, refusing low-trust agents
5. **DevSpot Agent Compatibility** — must provide `agent.json` and `agent_log.json`

**Fit:** Same ERC-8004 work as Agent Only. The market agent can register trades, verify analysis accuracy over time, and build a reputation score based on prediction quality.

### SECONDARY TARGETS (achievable with targeted integration)

#### 5. Crypto — $6,000 (1st $3K, 2nd $2K, 3rd $1K)
**Track description:** Onchain economies, programmable assets, privacy-first finance, agent-native commerce.
**Strategy:** Agent pays for premium data feeds via crypto. Publishes verified trade signals as programmable onchain assets. Token-gated access to analysis.

#### 6. Lit Protocol: NextGen AI Apps — prize TBD
**Strategy:** Encrypt premium investment signals behind Lit Protocol access conditions. Only token holders or subscribers can decrypt analysis reports. This is a killer demo of "AI + decentralized access control."
**Integration:** Lit SDK for encryption/decryption, access control conditions based on token holdings or NFT ownership.

#### 7. Storacha — prize TBD
**Strategy:** Store all execution logs, analysis reports, and audit trails on Storacha (IPFS/Filecoin layer). Verifiable, immutable record of every decision the agent made.
**Integration:** Storacha SDK for file upload/retrieval, content addressing.

### STRETCH TARGETS

#### 8. Starknet — prize TBD
**Strategy:** Verifiable computation proofs on the analysis pipeline. Prove the agent actually ran the analysis it claims to have run.

#### 9. NEAR: Best New or Continued Project — prize TBD
**Strategy:** Broad category. Just needs NEAR integration.

---

## New Repo Architecture

### Repo Name Suggestions
- `sovereign-market-agent`
- `autonomous-market-intelligence`
- `agent-alpha` (short, punchy)

### Directory Structure

```
/
├── agent.json                    # ERC-8004 capability manifest (REQUIRED)
├── agent_log.json                # Structured execution log (REQUIRED)
├── README.md                     # 250-500 word project summary
├── LICENSE                       # MIT or Apache-2
│
├── core/
│   ├── react_agent.py            # ReAct reasoning engine (from autoinvestor_react.py)
│   ├── decision_loop.py          # discover → plan → execute → verify → submit
│   ├── tool_registry.py          # Tool registration and orchestration
│   └── config.py                 # Environment and configuration
│
├── analysis/
│   ├── technical.py              # Technical indicators (SMA, RSI, MACD, BB)
│   ├── sentiment.py              # News sentiment analysis
│   ├── congressional.py          # Congressional trading patterns
│   ├── macro.py                  # Market regime detection (FRED)
│   ├── portfolio.py              # Correlation + diversification scoring
│   └── sector.py                 # Sector allocation risk
│
├── execution/
│   ├── order_executor.py         # Trade placement (Alpaca)
│   ├── risk_manager.py           # 8-layer safety system
│   ├── strategy.py               # Entry/exit logic
│   └── scanner.py                # Multi-ticker screening
│
├── integrations/
│   ├── erc8004/
│   │   ├── identity.py           # Agent identity registration
│   │   ├── reputation.py         # Reputation tracking + updates
│   │   └── verification.py       # Onchain transaction verification
│   ├── storacha/
│   │   ├── storage.py            # Execution log storage (IPFS/Filecoin)
│   │   └── retrieval.py          # Content-addressed retrieval
│   ├── lit_protocol/
│   │   ├── encryption.py         # Signal encryption
│   │   └── access_control.py     # Token-gated decryption conditions
│   └── alpaca/
│       ├── trading.py            # Paper + live trading
│       └── portfolio.py          # Position management
│
├── safety/
│   ├── guardrails.py             # Pre-action validation
│   ├── transaction_validator.py  # Parameter checking before irreversible actions
│   └── anomaly_detector.py       # Unsafe state detection
│
├── logging/
│   ├── structured_logger.py      # JSON execution log generator
│   ├── decision_recorder.py      # Decision tree capture
│   └── manifest_generator.py     # agent.json auto-generation
│
├── tests/
│   ├── test_decision_loop.py
│   ├── test_risk_manager.py
│   ├── test_erc8004.py
│   ├── test_storacha.py
│   └── test_lit_protocol.py
│
├── docs/
│   ├── architecture.md
│   ├── api_reference.md
│   ├── sponsor_integrations.md
│   └── safety_philosophy.md
│
├── demo/
│   └── demo_script.md            # Script for ≤3min YouTube video
│
├── requirements.txt
├── .env.example
└── Makefile
```

### agent.json Template (ERC-8004 Manifest)

```json
{
  "agent_name": "Sovereign",
  "version": "1.0.0",
  "operator_wallet": "<OPERATOR_ETH_ADDRESS>",
  "erc8004_identity": "<ERC8004_AGENT_ID>",
  "supported_tools": [
    "alpaca_trading",
    "yahoo_finance",
    "fred_macro_data",
    "congressional_trades",
    "news_sentiment",
    "technical_analysis",
    "storacha_storage",
    "lit_protocol_encryption"
  ],
  "supported_tech_stacks": ["python", "ethereum", "ipfs"],
  "compute_constraints": {
    "max_analysis_time_seconds": 120,
    "max_concurrent_tools": 5,
    "requires_gpu": false
  },
  "supported_task_categories": [
    "market_analysis",
    "trade_execution",
    "risk_assessment",
    "portfolio_optimization",
    "signal_generation"
  ],
  "safety_framework": {
    "layers": 8,
    "pre_trade_validation": true,
    "macro_aware_sizing": true,
    "sector_concentration_limits": true,
    "anomaly_detection": true
  }
}
```

### agent_log.json Template

```json
{
  "session_id": "<UUID>",
  "agent_id": "<ERC8004_AGENT_ID>",
  "timestamp_start": "2026-03-25T00:00:00Z",
  "timestamp_end": "2026-03-25T00:02:15Z",
  "decisions": [
    {
      "step": 1,
      "phase": "discover",
      "action": "scan_market_opportunities",
      "reasoning": "Identified elevated VIX with sector rotation signal",
      "tools_called": ["yahoo_finance", "fred_macro_data"],
      "result": "3 candidate tickers identified"
    },
    {
      "step": 2,
      "phase": "plan",
      "action": "multi_layer_analysis",
      "reasoning": "Running technical + fundamental + congressional + sentiment",
      "tools_called": ["technical_analysis", "congressional_trades", "news_sentiment"],
      "result": "TICKER_A: strong buy signal, risk-adjusted"
    },
    {
      "step": 3,
      "phase": "execute",
      "action": "place_trade",
      "reasoning": "Position size calculated via macro-adjusted model",
      "tools_called": ["alpaca_trading", "risk_manager"],
      "result": "Order placed: BUY 50 shares TICKER_A @ $XX.XX",
      "safety_checks": ["position_size_validated", "sector_concentration_ok", "macro_regime_checked"]
    },
    {
      "step": 4,
      "phase": "verify",
      "action": "confirm_execution_and_store",
      "reasoning": "Verifying fill, storing execution log to Storacha",
      "tools_called": ["alpaca_trading", "storacha_storage"],
      "result": "Fill confirmed. CID: bafy..."
    }
  ],
  "retries": [],
  "failures": [],
  "final_output": {
    "trades_executed": 1,
    "analysis_reports_generated": 1,
    "execution_log_cid": "bafy...",
    "reputation_updated": true
  }
}
```

---

## Integration Priority & Effort Estimates

| Integration | Challenges Unlocked | Effort | Priority |
|-------------|-------------------|--------|----------|
| ERC-8004 Identity + Reputation | Agent Only, Agents With Receipts | Medium | **P0** |
| agent.json + agent_log.json | Agent Only, Agents With Receipts | Low | **P0** |
| Storacha (execution log storage) | Fresh Code, Storacha, Infrastructure | Low-Medium | **P1** |
| Lit Protocol (signal encryption) | Lit Protocol, Crypto | Medium | **P1** |
| Autonomous mode (remove human loop) | Agent Only | Low | **P0** |
| Starknet (verifiable computation) | Starknet, Crypto | High | **P2 (stretch)** |

---

## Key Deadlines

- **Now → March 31:** Build, integrate, test
- **March 31:** Submissions due
- **Deliverables:** Public GitHub repo, ≤3min demo video (YouTube), 250-500 word summary, track selection

---

## Demo Video Script Outline (≤3 minutes)

1. **0:00–0:20** — Problem statement: Retail investors lack institutional-grade autonomous analysis
2. **0:20–0:50** — Architecture overview: ReAct agent with 8-layer safety, multi-source analysis
3. **0:50–1:30** — Live demo: Agent autonomously discovers opportunity, runs analysis, executes trade
4. **1:30–2:00** — Sponsor integrations: ERC-8004 identity registration, Storacha log storage, Lit Protocol encrypted signals
5. **2:00–2:30** — Safety & guardrails: Show agent_log.json, pre-trade validation, anomaly detection
6. **2:30–3:00** — Impact: Democratizing market intelligence, $0.05/analysis vs $24K/yr Bloomberg

---

## Sponsor SDK Integration Reference

### ERC-8004: Trustless Agents

**Spec:** https://eips.ethereum.org/EIPS/eip-8004
**Status:** Draft, but live on mainnet since Jan 29, 2026. 20K+ agents registered, 70+ projects building.
**Awesome list:** https://github.com/sudeepb02/awesome-erc8004

#### Three Registries

**1. Identity Registry** (ERC-721 based)
```solidity
// Register agent — returns agentId (tokenId)
function register(string agentURI, MetadataEntry[] calldata metadata) external returns (uint256 agentId)
function register(string agentURI) external returns (uint256 agentId)
function register() external returns (uint256 agentId)

// Update agent URI (points to agent registration JSON)
function setAgentURI(uint256 agentId, string calldata newURI) external

// Metadata key-value store
function getMetadata(uint256 agentId, string memory metadataKey) external view returns (bytes memory)
function setMetadata(uint256 agentId, string memory metadataKey, bytes memory metadataValue) external

// Agent wallet (separate from owner wallet)
function setAgentWallet(uint256 agentId, address newWallet, uint256 deadline, bytes calldata signature) external
function getAgentWallet(uint256 agentId) external view returns (address)
```

**Events:**
```solidity
event Registered(uint256 indexed agentId, string agentURI, address indexed owner)
event URIUpdated(uint256 indexed agentId, string newURI, address indexed updatedBy)
event MetadataSet(uint256 indexed agentId, string indexed indexedMetadataKey, string metadataKey, bytes metadataValue)
```

**Agent Registration File (agentURI must resolve to this JSON):**
```json
{
  "type": "https://eips.ethereum.org/EIPS/eip-8004#registration-v1",
  "name": "Sovereign",
  "description": "Autonomous market analysis with 8-layer safety",
  "image": "imageURL",
  "services": [
    {
      "name": "market_analysis",
      "endpoint": "https://agent.example.com/analyze",
      "version": "1.0.0"
    }
  ],
  "x402Support": false,
  "active": true,
  "registrations": [
    {
      "agentId": 0,
      "agentRegistry": "eip155:8453:0x8004A169FB4a3325136EB29fA0ceB6D2e539a432"
    }
  ],
  "supportedTrust": ["reputation"]
}
```

**2. Reputation Registry**
```solidity
// Give feedback on an agent
function giveFeedback(uint256 agentId, int128 value, uint8 valueDecimals,
  string calldata tag1, string calldata tag2, string calldata endpoint,
  string calldata feedbackURI, bytes32 feedbackHash) external

// Read reputation summary
function getSummary(uint256 agentId, address[] calldata clientAddresses,
  string tag1, string tag2) external view
  returns (uint64 count, int128 summaryValue, uint8 summaryValueDecimals)

// Read all feedback
function readAllFeedback(uint256 agentId, address[] calldata clientAddresses,
  string tag1, string tag2, bool includeRevoked) external view
  returns (address[] memory clients, uint64[] memory feedbackIndexes,
  int128[] memory values, uint8[] memory valueDecimals,
  string[] memory tag1s, string[] memory tag2s, bool[] memory revokedStatuses)
```

**3. Validation Registry**
```solidity
function validationRequest(address validatorAddress, uint256 agentId,
  string requestURI, bytes32 requestHash) external
function validationResponse(bytes32 requestHash, uint8 response,
  string responseURI, bytes32 responseHash, string tag) external
```

#### Known Deployments

| Network | Component | Address |
|---------|-----------|---------|
| Base L2 | Identity Registry | `0x8004A169FB4a3325136EB29fA0ceB6D2e539a432` |
| Base L2 | Helixa (alt registry) | `0x2e3B541C59D38b84E3Bc54e977200230A204Fe60` |
| Base L2 | ORIGIN Registry | `0xac62E9d0bE9b88674f7adf38821F6e8BAA0e59b0` |

#### Python SDK Options

| SDK | Install | Notes |
|-----|---------|-------|
| Praxis Python SDK | `pip install praxis-py-sdk` (TBD) | https://github.com/prxs-ai/praxis-py-sdk |
| M2M TRC-8004 SDK | `pip install trc8004-m2m` | TRON-based, may need adaptation |
| MolTrust MCP | `pip install moltrust-mcp-server` | MCP server with ERC-8004 identity |
| Helixa SDK (TS) | `npm install helixa-sdk` | TypeScript, Base L2 |
| Chitin MCP | `npx chitin-mcp-server` | MCP server with soul identity |

**Recommended approach for Python:** Use web3.py to interact directly with the Identity Registry contract on Base L2. The contract is standard ERC-721 + URIStorage — straightforward to call `register()`, `setAgentURI()`, and `setMetadata()` via web3.py.

```python
# Pseudocode for agent registration
from web3 import Web3

w3 = Web3(Web3.HTTPProvider("https://mainnet.base.org"))
identity_registry = w3.eth.contract(
    address="0x8004A169FB4a3325136EB29fA0ceB6D2e539a432",
    abi=IDENTITY_REGISTRY_ABI
)

# Register agent with URI pointing to agent.json on Storacha/IPFS
tx = identity_registry.functions.register(
    "ipfs://bafy.../agent.json"
).build_transaction({
    'from': operator_wallet,
    'nonce': w3.eth.get_transaction_count(operator_wallet),
    'gas': 300000,
})
signed = w3.eth.account.sign_transaction(tx, private_key)
tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
agent_id = receipt.logs[0].topics[1]  # Extract agentId from Registered event
```

---

### Storacha (IPFS/Filecoin Hot Storage)

**Docs:** https://docs.storacha.network/quickstart/
**AI-specific docs:** https://storacha.ai/

#### CLI Setup
```bash
npm install -g @storacha/cli    # Requires Node.js v18+, npm v7+
storacha login you@example.com  # Auth via email verification
storacha space create MyAgent   # Create a storage Space
```

#### Upload & Retrieve
```bash
# Upload a file — returns CID-based URL
storacha up agent_log.json
# => https://storacha.link/ipfs/bafybeiaad.../agent_log.json

# Retrieve via HTTP gateway
curl -L 'https://storacha.link/ipfs/[CID]/agent_log.json'

# Retrieve via IPFS peer-to-peer
ipfs cat [CID]/agent_log.json
```

#### JavaScript Client (for programmatic access)
```bash
npm install @storacha/client
```

#### Integration Plan for Market Agent
```
Agent completes analysis → generates agent_log.json
  → uploads to Storacha via CLI/SDK
  → receives CID (content-addressed hash)
  → stores CID in ERC-8004 metadata via setMetadata(agentId, "execution_log", cidBytes)
  → CID is permanent, immutable proof of what the agent did
```

**Key value prop for judges:** Every decision the agent makes is stored immutably on IPFS/Filecoin. The execution log CID is recorded onchain via ERC-8004 metadata. Full audit trail, zero trust required.

---

### Lit Protocol (Decentralized Encryption & Access Control)

**Docs:** https://developer.litprotocol.com
**Network:** Naga Mainnet (live)
**Python SDK announcement:** https://spark.litprotocol.com/lit-python-sdk/

#### Python SDK Installation
```bash
pip install lit-python-sdk
pip install agentWallet-python
```

#### Encrypt Data
```python
# Encrypt a market analysis report
encrypt_result = client.encrypt_string(
    data_to_encrypt="{ analysis: ... full JSON report ... }",
    access_control_conditions=access_control_conditions
)
# Returns: { "ciphertext": "...", "dataToEncryptHash": "..." }
```

#### Decrypt Data (requires meeting access conditions)
```python
decrypt_result = client.decrypt_string(
    ciphertext=encrypt_result["ciphertext"],
    data_to_encrypt_hash=encrypt_result["dataToEncryptHash"],
    chain="ethereum",
    access_control_conditions=access_control_conditions,
    session_sigs=session_sigs
)
```

#### Access Control Conditions (Token-Gating)
```python
# Example: Only holders of a specific ERC-20 token can decrypt
access_control_conditions = [
    {
        "contractAddress": "0x<TOKEN_CONTRACT>",
        "standardContractType": "ERC20",
        "chain": "ethereum",
        "method": "balanceOf",
        "parameters": [":userAddress"],
        "returnValueTest": {
            "comparator": ">=",
            "value": "1000000000000000000"  # 1 token (18 decimals)
        }
    }
]

# Example: Only holders of a specific NFT can decrypt
access_control_conditions = [
    {
        "contractAddress": "0x<NFT_CONTRACT>",
        "standardContractType": "ERC721",
        "chain": "ethereum",
        "method": "balanceOf",
        "parameters": [":userAddress"],
        "returnValueTest": {
            "comparator": ">=",
            "value": "1"
        }
    }
]
```

#### PKP (Programmable Key Pair) Generation
```python
mint_result = client.mint_with_auth(
    auth_method={
        "authMethodType": 1,
        "accessToken": auth_sig_result["authSig"],
    },
    scopes=[1]
)
pkp = mint_result["pkp"]
```

#### Lit Action Execution (Serverless Compute)
```python
js_code = """
(async () => {
    // Run arbitrary logic inside Lit's TEE network
    const result = await Lit.Actions.call({ ... });
    Lit.Actions.setResponse({response: JSON.stringify(result)});
})()
"""
result = client.execute_js(code=js_code, js_params={}, session_sigs=session_sigs)
```

#### Integration Plan for Market Agent
```
Agent generates premium analysis report
  → encrypts report via Lit Protocol with token-gating conditions
  → uploads encrypted ciphertext to Storacha (IPFS)
  → publishes CID + access conditions
  → only token holders can decrypt the premium signals
  → free tier: basic analysis (unencrypted)
  → premium tier: full analysis + congressional patterns + macro overlay (encrypted, token-gated)
```

**Key value prop for judges:** Decentralized paywall for AI-generated financial intelligence. No centralized server controls access — it's enforced cryptographically by Lit's MPC network.

---

## Implementation Sequence (7 days: March 25–31)

### Day 1 (March 25): Foundation
- [ ] Create new GitHub repo (Fresh Code eligible)
- [ ] Set up project structure per directory spec above
- [ ] Port core ReAct agent from AutoInvestor (clean rewrite, not fork)
- [ ] Port analysis modules (technical, sentiment, macro, congressional)
- [ ] Port risk manager (8-layer safety system)
- [ ] Implement structured logging (agent_log.json format)
- [ ] Write agent.json manifest

### Day 2 (March 26): Autonomous Mode
- [ ] Implement full autonomous decision loop (discover → plan → execute → verify)
- [ ] Remove all human-in-the-loop touchpoints
- [ ] Add scanner module (market opportunity discovery)
- [ ] Test end-to-end autonomous execution via Alpaca paper trading
- [ ] Generate sample agent_log.json from real execution

### Day 3 (March 27): ERC-8004 Integration
- [ ] Set up web3.py connection to Base L2
- [ ] Deploy agent registration (Identity Registry)
- [ ] Upload agent.json to Storacha, get CID, set as agentURI
- [ ] Implement reputation feedback after trade verification
- [ ] Test onchain transactions viewable on BaseScan

### Day 4 (March 28): Storacha Integration
- [ ] Install Storacha CLI, create Space
- [ ] Implement automatic upload of agent_log.json after each execution
- [ ] Store CIDs in ERC-8004 metadata
- [ ] Implement retrieval/verification of historical logs
- [ ] Test content-addressed audit trail

### Day 5 (March 29): Lit Protocol Integration
- [ ] Install lit-python-sdk
- [ ] Implement report encryption with access control conditions
- [ ] Define token-gating conditions (ERC-20 or ERC-721 based)
- [ ] Test encrypt/decrypt cycle
- [ ] Implement dual-tier output (free basic / encrypted premium)

### Day 6 (March 30): Polish & Documentation
- [ ] Write README (250-500 word summary)
- [ ] Write architecture.md, safety_philosophy.md
- [ ] Write sponsor_integrations.md detailing each integration
- [ ] Add comprehensive docstrings and type hints
- [ ] Run full end-to-end demo, capture output
- [ ] Fix any bugs, clean up code

### Day 7 (March 31): Demo & Submit
- [ ] Record ≤3min demo video
- [ ] Upload to YouTube
- [ ] Submit on DevSpot:
  - Select challenges: Fresh Code, AI & Robotics, Agent Only, Agents With Receipts, Crypto, Lit Protocol, Storacha
  - Attach GitHub repo URL
  - Attach YouTube video URL
  - Write 250-500 word project summary
  - List team members and social handles

---

## Open Questions

- [ ] Confirm Base L2 Identity Registry address `0x8004A169FB4a3325136EB29fA0ceB6D2e539a432` is active and accepting registrations
- [ ] Verify Storacha free tier limits (how many uploads before hitting a paywall?)
- [ ] Lit Protocol Naga network — confirm Python SDK works with Naga mainnet (not just Datil testnet)
- [ ] Alpaca paper trading sufficient for demo, or do judges want real trades?
- [ ] DevSpot submission UI — how many challenges can you actually select per submission?
- [ ] Do we need an ETH wallet with Base ETH for gas? If so, fund the operator wallet before Day 3
