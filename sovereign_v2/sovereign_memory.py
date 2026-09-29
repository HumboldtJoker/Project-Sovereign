"""Sovereign Memory — Mnemosyne Phase 1 (CMA core).

Remembers trades, theses, and retrieves relevant context for new
thesis generation. Phase 1 is text search + significance scoring.
Phase 2 adds the knowledge graph (PPR), Phase 3 adds temporal decay.

Usage:
    from sovereign_memory import SovereignMemory
    mem = SovereignMemory()
    mem.write_entry("XYZ", 100.00, "medium", thesis_text, signal_text, "Semiconductors")
    mem.write_exit("XYZ", 92.00, "stop", 3)
    context = mem.recall_for_thesis("AMD", "Semiconductors")
"""

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# Memory database. Every setting comes from the environment; the defaults
# match a local Postgres. SOVEREIGN_PG_PASSWORD is the name to use; the older
# CC_PG_PASSWORD is still read so an existing deployment keeps working.
PG_HOST = os.environ.get("SOVEREIGN_PG_HOST", "localhost")
PG_PORT = int(os.environ.get("SOVEREIGN_PG_PORT", "5434"))
PG_USER = os.environ.get("SOVEREIGN_PG_USER", "cc")
PG_PASS = os.environ.get("SOVEREIGN_PG_PASSWORD") or os.environ.get("CC_PG_PASSWORD", "")
PG_DB = os.environ.get("SOVEREIGN_PG_DB", "sovereign_memory")

MAX_CONTEXT_TOKENS = 400
MAX_TRADE_MEMORIES = 3
MAX_THESIS_MEMORIES = 2


def _connect():
    import psycopg2
    return psycopg2.connect(
        host=PG_HOST, port=PG_PORT,
        user=PG_USER, password=PG_PASS,
        # manage writes memory from inside the run lock; a hung database must
        # not hold the lock (and every stop behind it).
        connect_timeout=5, options="-c statement_timeout=5000",
        dbname=PG_DB,
    )


def _now():
    return datetime.now(timezone.utc)


_embed_model = None


def _embed(text):
    """Generate embedding for hybrid search. Returns None if unavailable."""
    global _embed_model
    try:
        from sentence_transformers import SentenceTransformer
        if _embed_model is None:
            _embed_model = SentenceTransformer("all-mpnet-base-v2")
        emb = _embed_model.encode(text, normalize_embeddings=True)
        return "[" + ",".join(str(float(x)) for x in emb) + "]"
    except Exception:
        return None


class SovereignMemory:
    """Phase 1 memory: trade + thesis storage and recall."""

    def __init__(self):
        self._conn = None

    def _get_conn(self):
        if self._conn is None or self._conn.closed:
            self._conn = _connect()
        return self._conn

    def write_entry(self, ticker, entry_price, conviction, thesis,
                    signal_breakdown, sector, composite_score=0.0,
                    direction="buy"):
        """Record a trade entry."""
        conn = self._get_conn()
        cur = conn.cursor()
        trade_id = f"trade_{uuid.uuid4().hex[:16]}"
        embed_text = f"{ticker} {sector} {direction} {conviction} {thesis}"
        embedding = _embed(embed_text)

        if embedding:
            cur.execute("""
                INSERT INTO trade_memories
                (id, ticker, direction, entry_price, composite_score,
                 conviction, thesis, signal_breakdown, sector, entry_date,
                 embedding)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector)
            """, (trade_id, ticker, direction, entry_price, composite_score,
                  conviction, thesis, signal_breakdown, sector, _now(),
                  embedding))
        else:
            cur.execute("""
                INSERT INTO trade_memories
                (id, ticker, direction, entry_price, composite_score,
                 conviction, thesis, signal_breakdown, sector, entry_date)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (trade_id, ticker, direction, entry_price, composite_score,
                  conviction, thesis, signal_breakdown, sector, _now()))

        conn.commit()
        log.info("Memory: entry recorded for %s (%s)", ticker, trade_id)

        try:
            from sovereign_kg import SovereignKG
            kg = SovereignKG()
            kg.index_trade(ticker, sector, conviction, trade_id=trade_id)
            kg.close()
        except Exception as e:
            log.debug("KG index failed: %s", e)

        return trade_id

    def write_exit(self, ticker, exit_price, exit_reason, hold_days,
                   entry_price=None):
        """Record a trade exit by updating the most recent open trade."""
        conn = self._get_conn()
        cur = conn.cursor()

        cur.execute("""
            SELECT id, entry_price FROM trade_memories
            WHERE ticker = %s AND exit_price IS NULL
            ORDER BY entry_date DESC LIMIT 1
        """, (ticker,))
        row = cur.fetchone()
        if not row:
            log.warning("Memory: no open trade found for %s exit", ticker)
            return

        trade_id, stored_entry = row
        entry = entry_price or stored_entry
        pnl = ((exit_price - entry) / entry * 100) if entry else 0

        cur.execute("""
            UPDATE trade_memories
            SET exit_price = %s, pnl_pct = %s, exit_reason = %s,
                hold_days = %s, exit_date = %s, significance = %s
            WHERE id = %s
        """, (exit_price, round(pnl, 2), exit_reason, hold_days, _now(),
              9 if exit_reason == "stop" else 7, trade_id))

        conn.commit()
        log.info("Memory: exit recorded for %s — %s (%.1f%%)", ticker,
                 exit_reason, pnl)

        try:
            from sovereign_kg import SovereignKG
            kg = SovereignKG()
            cur.execute("SELECT sector FROM trade_memories WHERE id = %s", (trade_id,))
            sector_row = cur.fetchone()
            kg.index_trade(ticker, sector_row[0] if sector_row else "",
                           "", exit_reason, trade_id=trade_id)
            kg.close()
        except Exception as e:
            log.debug("KG exit index failed: %s", e)

    def write_thesis(self, ticker, direction, conviction, thesis,
                     signal_breakdown, composite_score, sector,
                     was_executed=False):
        """Record a thesis (including vetoes/holds)."""
        conn = self._get_conn()
        cur = conn.cursor()
        thesis_id = f"thesis_{uuid.uuid4().hex[:16]}"

        cur.execute("""
            INSERT INTO thesis_memories
            (id, ticker, direction, conviction, thesis, signal_breakdown,
             composite_score, sector, was_executed, scan_date)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (thesis_id, ticker, direction, conviction, thesis,
              signal_breakdown, composite_score, sector, was_executed,
              _now()))

        conn.commit()
        return thesis_id

    def recall_for_thesis(self, ticker, sector=None):
        """Retrieve relevant memories for thesis generation.

        Returns a formatted context block (max ~400 tokens) containing:
        - Past trades on this ticker (if any)
        - Past trades on this sector (if any, and ticker has none)
        - Similar setups that resolved
        """
        conn = self._get_conn()
        cur = conn.cursor()
        blocks = []

        # 1. Past trades on this exact ticker
        cur.execute("""
            SELECT ticker, direction, entry_price, exit_price, pnl_pct,
                   hold_days, exit_reason, conviction, thesis, entry_date
            FROM trade_memories
            WHERE ticker = %s AND exit_price IS NOT NULL
            ORDER BY significance DESC, entry_date DESC
            LIMIT %s
        """, (ticker, MAX_TRADE_MEMORIES))
        ticker_trades = cur.fetchall()

        if ticker_trades:
            blocks.append(f"PAST TRADES ON {ticker}:")
            for t in ticker_trades:
                date_str = t[9].strftime("%Y-%m-%d") if t[9] else "?"
                pnl_str = f"{t[4]:+.1f}%" if t[4] is not None else "open"
                reason = t[6] or "open"
                thesis_short = (t[8] or "")[:120]
                blocks.append(
                    f"  [{date_str}] {t[1].upper()} at ${t[2]:.2f}, "
                    f"exited ${t[3]:.2f} ({reason}, {pnl_str}, "
                    f"{t[5]}d hold). {thesis_short}"
                )

        # 2. Past trades on same sector (if no ticker trades)
        if not ticker_trades and sector:
            cur.execute("""
                SELECT ticker, direction, entry_price, exit_price, pnl_pct,
                       hold_days, exit_reason, conviction, thesis, entry_date
                FROM trade_memories
                WHERE sector = %s AND exit_price IS NOT NULL
                ORDER BY significance DESC, entry_date DESC
                LIMIT %s
            """, (sector, MAX_TRADE_MEMORIES))
            sector_trades = cur.fetchall()

            if sector_trades:
                blocks.append(f"PAST TRADES IN {sector}:")
                for t in sector_trades:
                    date_str = t[9].strftime("%Y-%m-%d") if t[9] else "?"
                    pnl_str = f"{t[4]:+.1f}%" if t[4] is not None else "open"
                    reason = t[6] or "open"
                    blocks.append(
                        f"  [{date_str}] {t[0]} {t[1]} at ${t[2]:.2f} → "
                        f"${t[3]:.2f} ({reason}, {pnl_str})"
                    )

        # 3. Open trades on this ticker (current positions)
        cur.execute("""
            SELECT entry_price, conviction, thesis, entry_date
            FROM trade_memories
            WHERE ticker = %s AND exit_price IS NULL
            ORDER BY entry_date DESC LIMIT 1
        """, (ticker,))
        open_trade = cur.fetchone()
        if open_trade:
            date_str = open_trade[3].strftime("%Y-%m-%d") if open_trade[3] else "?"
            blocks.append(
                f"NOTE: You already have an open {ticker} position "
                f"from {date_str} at ${open_trade[0]:.2f} "
                f"({open_trade[1]} conviction)."
            )

        # 4. Recent stop-outs across all tickers (pattern warning)
        cur.execute("""
            SELECT ticker, pnl_pct, exit_date, thesis
            FROM trade_memories
            WHERE exit_reason = 'stop' AND exit_date > NOW() - INTERVAL '14 days'
            ORDER BY exit_date DESC LIMIT 3
        """, ())
        recent_stops = cur.fetchall()
        if recent_stops:
            blocks.append("RECENT STOPS (last 14 days):")
            for s in recent_stops:
                date_str = s[2].strftime("%Y-%m-%d") if s[2] else "?"
                blocks.append(f"  [{date_str}] {s[0]} {s[1]:+.1f}%")

        # 5. Knowledge graph associations
        try:
            from sovereign_kg import SovereignKG
            kg = SovereignKG()
            graph_context = kg.query_for_thesis(ticker, sector)
            if graph_context:
                blocks.append(graph_context)
            kg.close()
        except Exception as e:
            log.debug("KG query failed: %s", e)

        # Update access counts
        if ticker_trades:
            cur.execute("""
                UPDATE trade_memories SET access_count = access_count + 1,
                    last_accessed = %s
                WHERE ticker = %s AND exit_price IS NOT NULL
            """, (_now(), ticker))
            conn.commit()

        if not blocks:
            return ""

        header = "MEMORY (Sovereign's past experience — use to calibrate, not to blindly follow):\n"
        footer = "\nUse these memories to calibrate your conviction. Current price action always takes priority."
        return header + "\n".join(blocks) + footer

    def backfill_from_trade_log(self, trade_log_path):
        """Import existing trade_log.jsonl entries as memories."""
        path = Path(trade_log_path)
        if not path.exists():
            log.warning("Trade log not found: %s", path)
            return 0

        entries = {}
        imported = 0

        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue

            action = record.get("action")
            ticker = record.get("ticker")
            ts = record.get("timestamp", "")

            if action == "buy":
                entries[ticker] = record
                # Derive entry price from stop (stop = entry * (1 - stop_pct))
                stop = record.get("stop", 0)
                entry_price = stop / 0.92 if stop else 0
                self.write_entry(
                    ticker=ticker,
                    entry_price=round(entry_price, 2),
                    conviction=record.get("conviction", "low"),
                    thesis=record.get("thesis", ""),
                    signal_breakdown="",
                    sector="",
                    composite_score=record.get("composite", 0),
                )
                imported += 1

            elif action == "sell":
                why = record.get("why", "")
                if "STOP" in why.upper():
                    reason = "stop"
                elif "TARGET" in why.upper():
                    reason = "target"
                else:
                    reason = "manual"

                exit_price = 0
                entry_price = 0
                if ticker in entries:
                    buy = entries[ticker]
                    stop = buy.get("stop", 0)
                    entry_price = stop / 0.92 if stop else 0

                # Try to extract exit price from the why string
                import re
                price_match = re.search(r"(\d+\.?\d*)\s*[<>=]", why)
                if price_match:
                    exit_price = float(price_match.group(1))

                if exit_price and entry_price:
                    self.write_exit(ticker, exit_price, reason, 0, entry_price)
                    imported += 1

        log.info("Backfill complete: %d records imported", imported)
        return imported

    def close(self):
        if self._conn and not self._conn.closed:
            self._conn.close()
