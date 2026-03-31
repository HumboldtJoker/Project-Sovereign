"""
Sovereign — Live Dashboard

Real-time visualization of the autonomous decision loop.
FastAPI + WebSocket + single HTML page. No build tools.

Usage:
    python -m dashboard.app              # Dashboard only (port 8080)
    python -m dashboard.app --run        # Dashboard + autonomous agent
"""

import argparse
import asyncio
import json
import logging
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
import uvicorn

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logger = logging.getLogger(__name__)

app = FastAPI(title="Sovereign")

# ── Shared state ─────────────────────────────────────────────────────────────
state = {
    "session_id": "",
    "status": "idle",           # idle, running, complete, error
    "current_phase": "",        # discover, plan, execute, verify
    "decisions": [],
    "safety_checks": [],
    "portfolio": {},
    "failures": [],
    "started_at": "",
    "completed_at": "",
}

connected_clients: List[WebSocket] = []


async def broadcast(event: dict):
    """Send event to all connected WebSocket clients."""
    message = json.dumps(event, default=str)
    disconnected = []
    for ws in connected_clients:
        try:
            await ws.send_text(message)
        except Exception:
            disconnected.append(ws)
    for ws in disconnected:
        connected_clients.remove(ws)


def emit_sync(event: dict):
    """Thread-safe emit for use from the agent thread."""
    loop = asyncio.get_event_loop() if asyncio.get_event_loop().is_running() else None
    if loop:
        asyncio.run_coroutine_threadsafe(broadcast(event), loop)


# ── WebSocket endpoint ───────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    connected_clients.append(websocket)
    # Send current state on connect
    await websocket.send_text(json.dumps({"type": "state", "data": state}, default=str))
    try:
        while True:
            await websocket.receive_text()  # Keep alive
    except WebSocketDisconnect:
        connected_clients.remove(websocket)


# ── Dashboard HTML ───────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return DASHBOARD_HTML


@app.get("/api/state")
async def get_state():
    return state


@app.post("/api/run")
async def trigger_run():
    """Trigger an autonomous decision loop run."""
    if state["status"] == "running":
        return {"error": "Agent is already running"}

    thread = threading.Thread(target=_run_agent_thread, daemon=True)
    thread.start()
    return {"status": "started"}


def _run_agent_thread():
    """Run the agent in a background thread, emitting events to the dashboard."""
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_run_agent())


async def _run_agent():
    """Execute the autonomous decision loop with dashboard events."""
    from core.config import ANTHROPIC_API_KEY, ALPACA_PAPER

    state["session_id"] = str(uuid.uuid4())
    state["status"] = "running"
    state["decisions"] = []
    state["safety_checks"] = []
    state["failures"] = []
    state["started_at"] = datetime.now(timezone.utc).isoformat()
    state["completed_at"] = ""

    await broadcast({"type": "status", "data": "running"})

    if not ANTHROPIC_API_KEY:
        state["status"] = "error"
        state["failures"].append({"reason": "ANTHROPIC_API_KEY not set"})
        await broadcast({"type": "error", "data": "ANTHROPIC_API_KEY not set"})
        return

    try:
        from main import build_agent
        from core.decision_loop import DecisionLoop
        from audit_log.structured_logger import save_canonical_log

        agent = build_agent()
        loop = DecisionLoop(agent)

        # Monkey-patch the _record method to emit dashboard events
        original_record = loop._record

        def patched_record(phase, action, reasoning, tools_called, result, safety_checks=None):
            original_record(phase, action, reasoning, tools_called, result, safety_checks)

            decision = loop.decisions[-1]
            state["current_phase"] = phase
            state["decisions"] = loop.decisions

            if safety_checks:
                state["safety_checks"].extend(
                    {"check": c, "phase": phase, "timestamp": decision["timestamp"]}
                    for c in safety_checks
                )

            # Emit to dashboard
            asyncio.get_event_loop().create_task(broadcast({
                "type": "decision",
                "data": {
                    "phase": phase,
                    "action": action,
                    "reasoning": reasoning[:200],
                    "tools": tools_called,
                    "result": result[:300],
                    "safety_checks": safety_checks or [],
                    "step": decision["step"],
                    "timestamp": decision["timestamp"],
                },
            }))

            asyncio.get_event_loop().create_task(broadcast({
                "type": "phase",
                "data": phase,
            }))

        loop._record = patched_record

        # Run the loop
        result = loop.run()

        # Save log
        save_canonical_log(result)

        state["status"] = "complete"
        state["completed_at"] = datetime.now(timezone.utc).isoformat()
        state["failures"] = result.get("failures", [])

        await broadcast({"type": "status", "data": "complete"})
        await broadcast({"type": "result", "data": result})

    except Exception as e:
        logger.exception("Agent run failed: %s", e)
        state["status"] = "error"
        state["failures"].append({"reason": str(e)})
        await broadcast({"type": "error", "data": str(e)})


# ── Dashboard HTML ───────────────────────────────────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sovereign — Market Intelligence</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: 'SF Mono', 'Fira Code', 'Consolas', monospace;
    background: #0a0e17;
    color: #c8d3e0;
    min-height: 100vh;
  }
  .header {
    background: linear-gradient(135deg, #0d1321 0%, #1a1f35 100%);
    border-bottom: 1px solid #1e2a3a;
    padding: 20px 30px;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  .header h1 {
    font-size: 18px;
    color: #4fc3f7;
    font-weight: 600;
    letter-spacing: 1px;
  }
  .header .status {
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .status-dot {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    background: #546e7a;
    transition: background 0.3s;
  }
  .status-dot.running { background: #4fc3f7; animation: pulse 1.5s infinite; }
  .status-dot.complete { background: #66bb6a; }
  .status-dot.error { background: #ef5350; }
  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.4; }
  }
  .btn {
    background: #1a237e;
    color: #4fc3f7;
    border: 1px solid #283593;
    padding: 8px 20px;
    font-family: inherit;
    font-size: 13px;
    cursor: pointer;
    border-radius: 4px;
    transition: all 0.2s;
  }
  .btn:hover { background: #283593; }
  .btn:disabled { opacity: 0.4; cursor: not-allowed; }

  .main { display: grid; grid-template-columns: 280px 1fr; min-height: calc(100vh - 70px); }

  /* Phase sidebar */
  .phases {
    background: #0d1321;
    border-right: 1px solid #1e2a3a;
    padding: 20px;
  }
  .phases h2 {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 2px;
    color: #546e7a;
    margin-bottom: 20px;
  }
  .phase {
    padding: 15px;
    margin-bottom: 10px;
    border-radius: 6px;
    border: 1px solid #1e2a3a;
    background: #0f1525;
    transition: all 0.4s;
  }
  .phase .number {
    font-size: 11px;
    color: #37474f;
    margin-bottom: 4px;
  }
  .phase .name {
    font-size: 14px;
    font-weight: 600;
    color: #546e7a;
    transition: color 0.4s;
  }
  .phase .desc {
    font-size: 11px;
    color: #37474f;
    margin-top: 4px;
  }
  .phase.active {
    border-color: #4fc3f7;
    background: #0d1a2a;
    box-shadow: 0 0 20px rgba(79, 195, 247, 0.1);
  }
  .phase.active .name { color: #4fc3f7; }
  .phase.active .number { color: #4fc3f7; }
  .phase.done {
    border-color: #2e7d32;
    background: #0d1f15;
  }
  .phase.done .name { color: #66bb6a; }
  .phase.done .number { color: #66bb6a; }

  /* Safety section */
  .safety {
    margin-top: 30px;
    padding: 15px;
    border-radius: 6px;
    border: 1px solid #1e2a3a;
    background: #0f1525;
  }
  .safety h3 {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 2px;
    color: #546e7a;
    margin-bottom: 10px;
  }
  .safety-count {
    font-size: 24px;
    font-weight: 700;
    color: #66bb6a;
  }
  .safety-label {
    font-size: 11px;
    color: #546e7a;
  }

  /* Feed */
  .feed {
    padding: 20px 30px;
    overflow-y: auto;
    max-height: calc(100vh - 70px);
  }
  .feed h2 {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 2px;
    color: #546e7a;
    margin-bottom: 15px;
  }
  .event {
    margin-bottom: 12px;
    padding: 15px;
    border-radius: 6px;
    border: 1px solid #1e2a3a;
    background: #0d1321;
    animation: slideIn 0.3s ease-out;
  }
  @keyframes slideIn {
    from { opacity: 0; transform: translateY(-10px); }
    to { opacity: 1; transform: translateY(0); }
  }
  .event .meta {
    display: flex;
    justify-content: space-between;
    margin-bottom: 8px;
  }
  .event .phase-tag {
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 1px;
    padding: 2px 8px;
    border-radius: 3px;
    font-weight: 600;
  }
  .phase-tag.discover { background: #1a237e; color: #7986cb; }
  .phase-tag.plan { background: #4a148c; color: #ba68c8; }
  .phase-tag.execute { background: #b71c1c; color: #ef9a9a; }
  .phase-tag.verify { background: #1b5e20; color: #a5d6a7; }
  .phase-tag.conclude { background: #263238; color: #90a4ae; }
  .event .time {
    font-size: 10px;
    color: #37474f;
  }
  .event .action {
    font-size: 13px;
    color: #e0e0e0;
    margin-bottom: 6px;
  }
  .event .reasoning {
    font-size: 12px;
    color: #78909c;
    line-height: 1.5;
  }
  .event .tools {
    margin-top: 8px;
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
  }
  .tool-chip {
    font-size: 10px;
    padding: 2px 8px;
    border-radius: 3px;
    background: #1a1f35;
    color: #4fc3f7;
    border: 1px solid #1e2a3a;
  }
  .event .result {
    margin-top: 8px;
    font-size: 11px;
    color: #546e7a;
    background: #080c14;
    padding: 10px;
    border-radius: 4px;
    max-height: 120px;
    overflow-y: auto;
    white-space: pre-wrap;
    word-break: break-word;
  }
  .safety-checks {
    margin-top: 8px;
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
  }
  .check-chip {
    font-size: 10px;
    padding: 2px 8px;
    border-radius: 3px;
    background: #1b5e20;
    color: #a5d6a7;
  }

  .empty-state {
    text-align: center;
    padding: 60px 20px;
    color: #37474f;
  }
  .empty-state .icon { font-size: 48px; margin-bottom: 15px; }
  .empty-state p { font-size: 13px; line-height: 1.6; }
</style>
</head>
<body>

<div class="header">
  <h1>SOVEREIGN</h1>
  <div class="status">
    <div class="status-dot" id="statusDot"></div>
    <span id="statusText" style="font-size:12px; color:#546e7a">IDLE</span>
    <button class="btn" id="runBtn" onclick="startRun()">LAUNCH AGENT</button>
  </div>
</div>

<div class="main">
  <div class="phases">
    <h2>Decision Loop</h2>
    <div class="phase" id="phase-discover">
      <div class="number">PHASE 1</div>
      <div class="name">DISCOVER</div>
      <div class="desc">Scan markets for opportunities</div>
    </div>
    <div class="phase" id="phase-plan">
      <div class="number">PHASE 2</div>
      <div class="name">PLAN</div>
      <div class="desc">Multi-layer analysis</div>
    </div>
    <div class="phase" id="phase-execute">
      <div class="number">PHASE 3</div>
      <div class="name">EXECUTE</div>
      <div class="desc">Trade with safety guardrails</div>
    </div>
    <div class="phase" id="phase-verify">
      <div class="number">PHASE 4</div>
      <div class="name">VERIFY</div>
      <div class="desc">Confirm & store audit trail</div>
    </div>

    <div class="safety">
      <h3>Safety Guardrails</h3>
      <div class="safety-count" id="safetyCount">0</div>
      <div class="safety-label">checks passed</div>
    </div>
  </div>

  <div class="feed" id="feed">
    <h2>Execution Feed</h2>
    <div class="empty-state" id="emptyState">
      <div class="icon">&#9684;</div>
      <p>Agent is idle.<br>Click LAUNCH AGENT to start the autonomous decision loop.</p>
    </div>
  </div>
</div>

<script>
const ws = new WebSocket(`ws://${location.host}/ws`);
const feed = document.getElementById('feed');
const emptyState = document.getElementById('emptyState');
const phases = ['discover', 'plan', 'execute', 'verify'];
let completedPhases = new Set();
let safetyCount = 0;

ws.onmessage = (e) => {
  const msg = JSON.parse(e.data);

  if (msg.type === 'status') {
    setStatus(msg.data);
  } else if (msg.type === 'phase') {
    setActivePhase(msg.data);
  } else if (msg.type === 'decision') {
    addDecision(msg.data);
  } else if (msg.type === 'error') {
    addError(msg.data);
  } else if (msg.type === 'state') {
    // Initial state restore
    const s = msg.data;
    setStatus(s.status);
    if (s.current_phase) setActivePhase(s.current_phase);
    (s.decisions || []).forEach(d => addDecision(d));
  }
};

function setStatus(status) {
  const dot = document.getElementById('statusDot');
  const text = document.getElementById('statusText');
  const btn = document.getElementById('runBtn');
  dot.className = 'status-dot ' + status;
  text.textContent = status.toUpperCase();
  btn.disabled = status === 'running';
}

function setActivePhase(phase) {
  // Mark previous phases as done
  let found = false;
  phases.forEach(p => {
    const el = document.getElementById('phase-' + p);
    if (p === phase) {
      el.className = 'phase active';
      found = true;
    } else if (!found) {
      el.className = 'phase done';
      completedPhases.add(p);
    }
  });
}

function addDecision(d) {
  if (emptyState) emptyState.style.display = 'none';

  // Update safety count
  if (d.safety_checks && d.safety_checks.length > 0) {
    safetyCount += d.safety_checks.length;
    document.getElementById('safetyCount').textContent = safetyCount;
  }

  const el = document.createElement('div');
  el.className = 'event';

  const toolsHtml = (d.tools || []).map(t =>
    `<span class="tool-chip">${t}</span>`
  ).join('');

  const checksHtml = (d.safety_checks || []).map(c =>
    `<span class="check-chip">${c}</span>`
  ).join('');

  const time = d.timestamp ? new Date(d.timestamp).toLocaleTimeString() : '';

  el.innerHTML = `
    <div class="meta">
      <span class="phase-tag ${d.phase || ''}">${(d.phase || 'agent').toUpperCase()}</span>
      <span class="time">${time}</span>
    </div>
    <div class="action">${d.action || ''}</div>
    <div class="reasoning">${d.reasoning || ''}</div>
    ${toolsHtml ? '<div class="tools">' + toolsHtml + '</div>' : ''}
    ${d.result ? '<div class="result">' + escapeHtml(d.result) + '</div>' : ''}
    ${checksHtml ? '<div class="safety-checks">' + checksHtml + '</div>' : ''}
  `;

  // Insert after the h2
  const h2 = feed.querySelector('h2');
  if (h2.nextSibling) {
    feed.insertBefore(el, h2.nextSibling);
  } else {
    feed.appendChild(el);
  }
}

function addError(msg) {
  if (emptyState) emptyState.style.display = 'none';
  const el = document.createElement('div');
  el.className = 'event';
  el.style.borderColor = '#b71c1c';
  el.innerHTML = `<div class="action" style="color:#ef5350">ERROR: ${escapeHtml(msg)}</div>`;
  const h2 = feed.querySelector('h2');
  if (h2.nextSibling) feed.insertBefore(el, h2.nextSibling);
  else feed.appendChild(el);
}

function startRun() {
  safetyCount = 0;
  document.getElementById('safetyCount').textContent = '0';
  completedPhases.clear();
  phases.forEach(p => document.getElementById('phase-' + p).className = 'phase');
  // Clear old events
  feed.querySelectorAll('.event').forEach(e => e.remove());
  if (emptyState) emptyState.style.display = 'none';

  fetch('/api/run', { method: 'POST' });
}

function escapeHtml(s) {
  const div = document.createElement('div');
  div.textContent = s;
  return div.innerHTML;
}
</script>
</body>
</html>
"""


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sovereign Agent Dashboard")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--run", action="store_true", help="Auto-launch agent on startup")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    if args.run:
        # Auto-trigger after server starts
        import time

        def auto_run():
            time.sleep(2)
            import requests
            try:
                requests.post(f"http://localhost:{args.port}/api/run")
            except Exception:
                pass

        threading.Thread(target=auto_run, daemon=True).start()

    uvicorn.run(app, host="0.0.0.0", port=args.port)
