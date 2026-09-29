"""Sovereign alerts — Discord webhook (+ optional SMTP) for high-conviction events.

Config (environment):
  DISCORD_WEBHOOK_URL   Discord channel webhook. If unset, alerts log-only.
  SOVEREIGN_SMTP_HOST / _PORT / _USER / _PASS / _TO   optional email fallback.

Deduped per (title, day) so a 30-minute cron loop doesn't spam the channel.

Usage:  python3 sovereign_alerts.py test
"""

import hashlib
import logging
import os
import smtplib
import sys
from datetime import datetime
from email.mime.text import MIMEText

import requests

from sovereign_config import STATE_DIR, load_json, save_json

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("alerts")

SENT_FILE = STATE_DIR / "alerts_sent.json"

LEVEL_COLORS = {"info": 0x3498DB, "signal": 0x2ECC71, "warning": 0xE67E22, "critical": 0xE74C3C}


def _already_sent(key: str) -> bool:
    today = datetime.now().strftime("%Y-%m-%d")
    sent = load_json(SENT_FILE, {})
    if sent.get("date") != today:
        sent = {"date": today, "keys": []}
    if key in sent["keys"]:
        return True
    sent["keys"].append(key)
    save_json(SENT_FILE, sent)
    return False


def _send_discord(title: str, lines: list[str], level: str) -> bool:
    url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not url:
        return False
    payload = {
        "embeds": [{
            "title": f"🏦 Sovereign — {title}",
            "description": "\n".join(lines)[:3900],
            "color": LEVEL_COLORS.get(level, LEVEL_COLORS["info"]),
            "footer": {"text": datetime.now().strftime("%Y-%m-%d %H:%M PT")},
        }]
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        return True
    except Exception as e:
        log.warning("Discord alert failed: %s", e)
        return False


def _send_email(title: str, lines: list[str]) -> bool:
    host = os.environ.get("SOVEREIGN_SMTP_HOST", "").strip()
    to = os.environ.get("SOVEREIGN_SMTP_TO", "").strip()
    if not host or not to:
        return False
    try:
        msg = MIMEText("\n".join(lines))
        msg["Subject"] = f"[Sovereign] {title}"
        msg["From"] = os.environ.get("SOVEREIGN_SMTP_USER", "sovereign@localhost")
        msg["To"] = to
        with smtplib.SMTP(host, int(os.environ.get("SOVEREIGN_SMTP_PORT", "587")), timeout=15) as s:
            s.starttls()
            user = os.environ.get("SOVEREIGN_SMTP_USER", "")
            pw = os.environ.get("SOVEREIGN_SMTP_PASS", "")
            if user and pw:
                s.login(user, pw)
            s.send_message(msg)
        return True
    except Exception as e:
        log.warning("Email alert failed: %s", e)
        return False


def alert(title: str, lines: list[str], level: str = "info", dedupe: bool = True):
    """Fire an alert through every configured channel. Always logs. Never
    raises: alerts are called from the paths that report a full disk or a
    broken broker, and an alert that raised there (its dedupe file write
    failing on the same full disk) aborted manage before any stop went out."""
    try:
        key = hashlib.sha256(f"{title}|{level}".encode()).hexdigest()[:16]
        try:
            if dedupe and _already_sent(key):
                log.info("Alert deduped: %s", title)
                return
        except Exception as e:           # cannot record the dedupe: send anyway
            log.warning("Alert dedupe unavailable (%s); sending", e)
        log.info("ALERT [%s] %s\n%s", level.upper(), title, "\n".join(str(l) for l in lines))
        sent_discord = _send_discord(title, lines, level)
        sent_email = _send_email(title, lines)
        if not sent_discord and not sent_email:
            log.info("No alert channel configured (set DISCORD_WEBHOOK_URL) — logged only.")
    except Exception as e:
        try:
            log.error("Alert failed entirely (%s): %s", e, title)
        except Exception:
            pass


def alert_high_conviction(signals: dict, theses: list = None):
    """Called by the pipeline after a scan. signals: {ticker: CompositeSignal}."""
    hot = {t: s for t, s in signals.items() if s.conviction in ("high", "medium") and s.score > 0}
    if not hot:
        return
    lines = []
    thesis_by_ticker = {t.ticker: t for t in (theses or [])}
    for ticker, s in sorted(hot.items(), key=lambda kv: kv[1].score, reverse=True):
        lines.append(f"**{ticker}** composite {s.score:+.2f} ({s.conviction.upper()})")
        top = sorted(s.components, key=lambda c: abs(c.contribution), reverse=True)[:3]
        for c in top:
            if c.detail:
                lines.append(f"  · {c.name}: {c.detail}")
        th = thesis_by_ticker.get(ticker)
        if th and th.direction == "buy":
            lines.append(f"  · thesis: entry ${th.entry_price:.2f}, stop ${th.stop_loss:.2f}, "
                         f"target ${th.target:.2f}")
        for f in s.risk_flags:
            lines.append(f"  ⚠️ {f}")
    alert(f"{len(hot)} high-conviction signal{'s' if len(hot) > 1 else ''}",
          lines, level="signal")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        alert("Test alert", ["Sovereign alert channel is working.",
                             "If you see this in Discord, the webhook is wired."],
              level="info", dedupe=False)
    else:
        print("Usage: python3 sovereign_alerts.py test")
