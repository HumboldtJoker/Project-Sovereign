# Sponsor Integrations

## ERC-8004: Trustless Agent Identity (Ethereum Foundation)

**What we built**: On-chain agent identity and reputation system on Base L2.

**How it works**:
1. Agent registers on the ERC-8004 Identity Registry, receiving an NFT-based `agentId`
2. The agent's `agentURI` points to its capability manifest (agent.json) stored on IPFS via Storacha
3. After each verified trade, the agent submits reputation feedback on-chain
4. Over time, the agent builds a verifiable track record: prediction accuracy, risk management quality, and execution reliability — all auditable by anyone

**Why it matters**: An autonomous trading agent without verifiable identity is a black box. ERC-8004 turns it into an accountable agent with a public track record. Other agents, validators, or users can query reputation before trusting its signals.

**Files**: `integrations/erc8004/identity.py`, `integrations/erc8004/reputation.py`

---

## Storacha: Immutable Audit Trail (IPFS/Filecoin)

**What we built**: Every execution log is content-addressed and stored immutably.

**How it works**:
1. After each decision loop, the structured `agent_log.json` is uploaded to Storacha
2. Storacha returns a CID (Content Identifier) — a cryptographic hash of the content
3. The CID is stored as ERC-8004 metadata on-chain, linking the agent's identity to its execution history
4. Anyone can retrieve the log via the CID and verify it matches the on-chain reference

**Why it matters**: "Trust but verify" requires verification to be possible. With Storacha, every decision the agent made is permanently retrievable. The CID guarantees the log hasn't been tampered with. This is the difference between "we say the agent is safe" and "here's cryptographic proof of what the agent actually did."

**Files**: `integrations/storacha/storage.py`, `integrations/storacha/retrieval.py`

---

## Lit Protocol: Decentralized Signal Encryption

**What we built**: Two-tier signal distribution with cryptographic access control.

**How it works**:
1. The agent classifies each analysis report as "free" or "premium" based on content depth
2. Free tier (basic technical analysis) is published unencrypted
3. Premium tier (congressional patterns, macro overlay, composite signals) is encrypted via Lit Protocol
4. Decryption requires meeting on-chain conditions: holding a specific ERC-20 token or NFT
5. No centralized server controls access — it's enforced by Lit's MPC network

**Why it matters**: This is a decentralized paywall for AI-generated financial intelligence. The agent can monetize its premium signals without a payment processor, subscription service, or centralized infrastructure. Token holders get access; everyone else gets the free tier. The access conditions are transparent and enforceable by math, not terms of service.

**Files**: `integrations/lit_protocol/encryption.py`, `integrations/lit_protocol/access_control.py`

---

## Integration Synergy

These three integrations create a closed loop:

```
Agent acts → Storacha stores proof → ERC-8004 links proof to identity
                                          ↓
                                    Reputation grows
                                          ↓
                              Lit Protocol gates premium access
                                          ↓
                              Token holders fund the agent
                                          ↓
                                    Agent acts again
```

The agent builds its own reputation, monetizes its intelligence, and proves its work — all on decentralized infrastructure with no central point of failure.
