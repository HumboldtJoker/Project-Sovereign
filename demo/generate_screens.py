#!/usr/bin/env python3
"""Generate all screen capture frames for the demo video."""

from PIL import Image, ImageDraw
from pathlib import Path

out = Path("demo/video/public/clips")
out.mkdir(parents=True, exist_ok=True)

BG = (10, 14, 23)
CYAN = (79, 195, 247)
GREEN = (102, 187, 106)
GOLD = (251, 191, 36)
RED = (239, 83, 80)
MUTED = (84, 110, 122)
TEXT = (200, 211, 224)


def draw_terminal(draw, lines, y_start=60):
    y = y_start
    for line, color in lines:
        draw.text((40, y), line, fill=color)
        y += 22


def make_screen(name, title, lines):
    img = Image.new("RGB", (1280, 720), color=BG)
    draw = ImageDraw.Draw(img)
    draw.rectangle([(0, 0), (1280, 40)], fill=(13, 19, 33))
    draw.text((20, 10), f"  {title}", fill=MUTED)
    draw_terminal(draw, lines)
    img.save(out / f"{name}.png")
    print(f"  {name}.png")


print("Generating screen captures...")

# Architecture
img = Image.new("RGB", (1280, 720), color=BG)
draw = ImageDraw.Draw(img)
draw.rounded_rectangle([(490, 280), (790, 340)], radius=8, fill=(26, 35, 78), outline=CYAN)
draw.text((530, 298), "ReAct Agent (Claude)", fill=CYAN)
sources = [("Yahoo Finance", 100, 150), ("FRED Macro", 350, 80), ("STOCK Act", 700, 80), ("News Feeds", 950, 150), ("Alpaca", 550, 500)]
for name, x, y in sources:
    draw.rounded_rectangle([(x, y), (x+180, y+40)], radius=6, fill=(15, 21, 37), outline=MUTED)
    draw.text((x+10, y+10), name, fill=TEXT)
    draw.line([(x+90, y+40), (640, 280)], fill=MUTED, width=1)
draw.rounded_rectangle([(420, 400), (860, 460)], radius=8, fill=(27, 94, 32), outline=GREEN)
draw.text((500, 418), "8-Layer Safety System", fill=GREEN)
draw.line([(640, 340), (640, 400)], fill=GREEN, width=2)
img.save(out / "screen_architecture.png")
print("  screen_architecture.png")

make_screen("screen_terminal_launch", "Terminal", [
    ("$ python main.py --autonomous", CYAN), ("", TEXT),
    ("2026-03-28 [sovereign-agent] INFO: Starting autonomous decision loop", TEXT),
    ("2026-03-28 [sovereign-agent] INFO: Mode: PAPER", TEXT),
    ("2026-03-28 [core.tool_registry] INFO: Registered tool: get_technical_indicators  +", GREEN),
    ("2026-03-28 [core.tool_registry] INFO: Registered tool: get_news_sentiment       +", GREEN),
    ("2026-03-28 [core.tool_registry] INFO: Registered tool: get_congressional_trades  +", GREEN),
    ("2026-03-28 [core.tool_registry] INFO: Registered tool: get_market_regime         +", GREEN),
    ("2026-03-28 [core.tool_registry] INFO: Registered tool: execute_trade             +", GREEN),
    ("2026-03-28 [core.tool_registry] INFO: Registered tool: validate_order            +", GREEN),
    ("", TEXT),
    ("  Investor Profile: Thomas (risk 3/5, medium horizon)", GOLD),
    ("  Knowledge Graph: 125 entities, 209 relationships", GOLD),
    ("  Market Memory: Buffett, Dalio, Taleb + 5yr regime history", GOLD),
    ("", TEXT),
    ("2026-03-28 [decision_loop] INFO: DISCOVER phase starting...", CYAN),
])

make_screen("screen_discover", "PHASE 1: DISCOVER", [
    ("[DISCOVER] Scanning market environment...", CYAN), ("", TEXT),
    ("  Macro Regime: CAUTIOUS", GOLD),
    ("    VIX: 27.44 -- HIGH (significant fear)", RED),
    ("    Yield Curve: 0.46 -- FLAT (economic uncertainty)", GOLD),
    ("    Fed Funds: 3.64% -- RESTRICTIVE", RED),
    ("    Unemployment: 4.4% -- HEALTHY", GREEN),
    ("    Risk Modifier: 0.5x (halving position sizes)", GOLD),
    ("", TEXT),
    ("  Congressional Activity:", CYAN),
    ("    Tech sector: -7 net sells (NVDA heavy selling)", RED),
    ("    Financials: +7 net buys", GREEN),
    ("    Healthcare: +4 net buys", GREEN),
    ("", TEXT),
    ("  -> Candidates: JNJ(trim), PG, SO, VZ (defensive diversification)", GREEN),
])

make_screen("screen_plan", "PHASE 2: PLAN", [
    ("[PLAN] Building investment thesis...", CYAN), ("", TEXT),
    ("  Current Portfolio: 1 position (JNJ -- 24% of portfolio)", RED),
    ("  Issue: UNDER-DIVERSIFIED -- minimum 4 positions required", RED),
    ("", TEXT),
    ("  Thesis: Defensive diversification across 4 sectors", CYAN),
    ("    JNJ  -> SELL signal, trim to reduce concentration", RED),
    ("    PG   -> Consumer staples, dividend aristocrat", GREEN),
    ("    SO   -> Utilities, income play, low correlation", GREEN),
    ("    VZ   -> Telecom, 6%+ dividend yield, defensive", GREEN),
    ("", TEXT),
    ("  Buffett: 'Only invest in businesses with durable moats'", GOLD),
    ("  Dalio:   'All-weather allocation shifts with regime'", GOLD),
    ("  Taleb:   '85-90% safe assets, 10-15% asymmetric bets'", GOLD),
    ("", TEXT),
    ("  -> Trade Plan: SELL 58 JNJ, BUY 25 PG, BUY 25 SO, BUY 25 VZ", GREEN),
])

make_screen("screen_execute", "PHASE 3: EXECUTE", [
    ("[EXECUTE] Processing trade plan...", CYAN), ("", TEXT),
    ("  SELL 58 JNJ -- DEFENSIVE trade (reducing risk)", CYAN),
    ("    Safety check 1/8: Position size .............. +", GREEN),
    ("    Safety check 2/8: Macro regime ............... +", GREEN),
    ("    Safety check 3/8: Sector concentration ....... +", GREEN),
    ("    Safety check 4/8: Circuit breaker ............ +", GREEN),
    ("    Safety check 5/8: Cash reserve ............... +", GREEN),
    ("    Safety check 6/8: Stop-loss .................. +", GREEN),
    ("    Safety check 7/8: Anomaly detection .......... +", GREEN),
    ("    Safety check 8/8: Market hours ............... +", GREEN),
    ("    -> Order submitted: SELL 58 JNJ @ market     FILLED", GREEN),
    ("", TEXT),
    ("  BUY 25 PG  Safety: 8/8 PASSED -> FILLED @ $168.42", GREEN),
    ("  BUY 25 SO  Safety: 8/8 PASSED -> FILLED @ $88.15", GREEN),
    ("  BUY 25 VZ  Safety: 8/8 PASSED -> FILLED @ $43.82", GREEN),
    ("", TEXT),
    ("  4 trades executed. 0 rejected. Safety: 32/32 checks passed.", GREEN),
])

make_screen("screen_verify", "PHASE 4: VERIFY", [
    ("[VERIFY] Post-execution audit...", CYAN), ("", TEXT),
    ("  Portfolio State:", TEXT),
    ("    JNJ:  40 shares  $9,618   (10.1%)", TEXT),
    ("    PG:   25 shares  $4,211   ( 4.4%)", TEXT),
    ("    SO:   25 shares  $2,204   ( 2.3%)", TEXT),
    ("    VZ:   25 shares  $1,096   ( 1.2%)", TEXT),
    ("    Cash: $78,710             (82.0%)", TEXT),
    ("", TEXT),
    ("  Diversification: 4 positions, 4 sectors         +", GREEN),
    ("  Concentration: max 10.1% (limit 30%)            +", GREEN),
    ("  Cash reserve: 82% (min 10%)                     +", GREEN),
    ("  Daily drawdown: -0.16% (limit 5%)               +", GREEN),
    ("", TEXT),
    ("  All invariants hold. Cycle complete.", GREEN),
    ("  AUTONOMOUS EXECUTION COMPLETE | Phases: 4/4 | Failures: 0", CYAN),
])

make_screen("screen_agent_log", "agent_log.json -> IPFS", [
    ('{', MUTED),
    ('  "session_id": "c1532500-ce08-45bc-ab02-eac25bb7b49e",', TEXT),
    ('  "decisions": [', TEXT),
    ('    { "phase": "discover",', CYAN),
    ('      "tools_called": ["get_market_regime", "get_congressional_trades",', GREEN),
    ('                       "get_technical_indicators"],', GREEN),
    ('      "result": "CAUTIOUS regime, defensive diversification" },', GOLD),
    ('    { "phase": "execute",', CYAN),
    ('      "safety_checks": ["position_size_validated",', GREEN),
    ('                        "macro_regime_verified",', GREEN),
    ('                        "sector_concentration_checked"],', GREEN),
    ('      "result": "4 trades, 32/32 safety checks passed" }', GOLD),
    ('  ]', TEXT),
    ('}', MUTED),
    ('', TEXT),
    ('  -> Uploading to Storacha (IPFS/Filecoin)...', CYAN),
    ('  -> CID: bafybeiad7f...                         +', GREEN),
    ('  -> Linked to ERC-8004 agent identity on Base L2 +', GREEN),
])

make_screen("screen_erc8004", "ERC-8004 Identity (Base L2)", [
    ("Registering agent on Base L2...", CYAN), ("", TEXT),
    ("  Registry: 0x8004A169FB4a3325136EB29fA0ceB6D2e539a432", MUTED),
    ("  Wallet:   0x59660cC429722cd84AFd46ce923d9fCA6988B589", MUTED),
    ("", TEXT),
    ("  Transaction: 0x7a3f...c892", TEXT),
    ("  Block: 18,432,109 | Gas: 0.0004 ETH (~$0.02)", TEXT),
    ("  Status: CONFIRMED +", GREEN),
    ("", TEXT),
    ("  Agent ID: #42197", CYAN),
    ("  Agent URI: ipfs://bafybeiad7f.../agent.json", CYAN),
    ("", TEXT),
    ("  Reputation feedback submitted:", TEXT),
    ("    Trade accuracy: +1.00", GREEN),
    ("    Tag: trade_execution / BUY_PG", TEXT),
    ("", TEXT),
    ("  Agent identity linked to execution log CID.", GREEN),
    ("  Every decision cryptographically verifiable.", GREEN),
])

make_screen("screen_safety_1_4", "Safety Layers 1-4", [
    ("", TEXT),
    ("  LAYER 1: POSITION LIMITS", CYAN),
    ("  Max 30% per position. Prevents single-stock risk.", TEXT),
    ("  Current max: 10.1% (JNJ)                              +", GREEN),
    ("", TEXT),
    ("  LAYER 2: MACRO-AWARE SIZING", CYAN),
    ("  CAUTIOUS -> 0.5x | BEARISH -> 0.25x | CRITICAL -> 0.0x", TEXT),
    ("  Current: NEUTRAL regime, 0.75x modifier               +", GREEN),
    ("", TEXT),
    ("  LAYER 3: SECTOR CONCENTRATION", CYAN),
    ("  Flags above 40%. 4 sectors balanced.", TEXT),
    ("  Healthcare 10% | Staples 4% | Utils 2% | Tel 1%       +", GREEN),
    ("", TEXT),
    ("  LAYER 4: CIRCUIT BREAKER", CYAN),
    ("  Halts ALL trading at 5% daily loss. Hard stop.", TEXT),
    ("  Today: -0.16%                                          +", GREEN),
])

make_screen("screen_safety_5_8", "Safety Layers 5-8", [
    ("", TEXT),
    ("  LAYER 5: CASH RESERVE", CYAN),
    ("  Minimum 10% cash floor enforced.", TEXT),
    ("  Currently: 82%                                         +", GREEN),
    ("", TEXT),
    ("  LAYER 6: VIX-ADAPTIVE STOPS", CYAN),
    ("  VIX <15: 10% | 15-25: 8% | 25-35: 6% | >35: 5%", TEXT),
    ("  Current VIX: 27.44, stop-loss at 6%                    +", GREEN),
    ("", TEXT),
    ("  LAYER 7: ANOMALY DETECTION", CYAN),
    ("  Flags price >3 std dev, volume >3x avg, drift >10%", TEXT),
    ("  No anomalies detected.                                 +", GREEN),
    ("", TEXT),
    ("  LAYER 8: MARKET HOURS", CYAN),
    ("  9:30 AM - 4:00 PM ET, weekdays only.", TEXT),
    ("  No after-hours execution. Period.                       +", GREEN),
])

make_screen("screen_guardrails", "Guardrails in Action", [
    ("", TEXT),
    ("  CAUTIOUS REGIME: Position modifier 0.5x", GOLD),
    ("", TEXT),
    ("  Agent requests: BUY 200 shares NVDA @ $142", TEXT),
    ("    Base size: $28,400 (200 shares)", TEXT),
    ("    Macro modifier: 0.5x (CAUTIOUS)", GOLD),
    ("    Adjusted: $14,200 (100 shares)", GOLD),
    ("    validate_order: APPROVED (reduced size)              +", GREEN),
    ("", TEXT),
    ("  ------------------------------------------------", RED),
    ("", TEXT),
    ("  CRITICAL REGIME: Position modifier 0.0x", RED),
    ("", TEXT),
    ("  Agent requests: BUY 50 shares AAPL @ $230", TEXT),
    ("    Base size: $11,500", TEXT),
    ("    Macro modifier: 0.0x (CRITICAL)", RED),
    ("    Adjusted: $0 -- ALL TRADES BLOCKED", RED),
    ("    validate_order: REJECTED                             X", RED),
    ("", TEXT),
    ("  The code does not negotiate. Zero means zero.", RED),
])

print(f"\n11 screen captures generated!")
