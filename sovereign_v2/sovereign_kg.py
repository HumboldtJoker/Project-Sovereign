"""Sovereign Knowledge Graph — Mnemosyne Phase 2.

Deterministic entity extraction from structured trading data + graph
traversal via Personalized PageRank (PPR) for associative retrieval.

No spaCy NER — trading entities (tickers, sectors, members, regimes,
outcomes) are already structured. Extraction is a mapping, not inference.

Usage:
    from sovereign_kg import SovereignKG
    kg = SovereignKG()
    kg.index_trade("XYZ", "Semiconductors", "medium", "stop",
                   members=["Cisneros", "Gottheimer"], regime="RISK_ON")
    related = kg.query("AMD", context_entities=["Semiconductors"])
"""

import logging
import os
from collections import defaultdict

log = logging.getLogger(__name__)

# Memory database. Every setting comes from the environment; the defaults
# match a local Postgres. SOVEREIGN_PG_PASSWORD is the name to use; the older
# CC_PG_PASSWORD is still read so an existing deployment keeps working.
PG_HOST = os.environ.get("SOVEREIGN_PG_HOST", "localhost")
PG_PORT = int(os.environ.get("SOVEREIGN_PG_PORT", "5434"))
PG_USER = os.environ.get("SOVEREIGN_PG_USER", "cc")
PG_PASS = os.environ.get("SOVEREIGN_PG_PASSWORD") or os.environ.get("CC_PG_PASSWORD", "")
PG_DB = os.environ.get("SOVEREIGN_PG_DB", "sovereign_memory")


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


class SovereignKG:
    """Knowledge graph for trading entity relationships."""

    def __init__(self):
        self._conn = None

    def _get_conn(self):
        if self._conn is None or self._conn.closed:
            self._conn = _connect()
        return self._conn

    def _ensure_entity(self, name, entity_type, metadata=None):
        """Get or create an entity, return its ID."""
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT id FROM kg_entities WHERE name = %s AND entity_type = %s",
            (name, entity_type),
        )
        row = cur.fetchone()
        if row:
            return row[0]

        import json
        cur.execute(
            """INSERT INTO kg_entities (name, entity_type, metadata)
               VALUES (%s, %s, %s) RETURNING id""",
            (name, entity_type, json.dumps(metadata or {})),
        )
        conn.commit()
        return cur.fetchone()[0]

    def _add_triple(self, subject_id, predicate, object_id, weight=1.0,
                    source_memory_id=None):
        """Add a directed edge. Upserts weight if edge exists."""
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO kg_triples (subject_id, predicate, object_id, weight, source_memory_id)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT DO NOTHING""",
            (subject_id, predicate, object_id, weight, source_memory_id),
        )
        conn.commit()

    def _add_mention(self, entity_id, memory_table, memory_id):
        """Link an entity to the memory that mentions it."""
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO kg_entity_mentions (entity_id, memory_table, memory_id)
               VALUES (%s, %s, %s)""",
            (entity_id, memory_table, memory_id),
        )
        conn.commit()

    # ── Deterministic extraction + indexing ────────────────────────

    def index_trade(self, ticker, sector, conviction, exit_reason=None,
                    members=None, regime=None, trade_id=None):
        """Extract entities from a trade and build graph edges."""
        ticker_id = self._ensure_entity(ticker, "TICKER")
        sector_id = self._ensure_entity(sector, "SECTOR") if sector else None
        outcome_id = self._ensure_entity(exit_reason, "OUTCOME") if exit_reason else None
        regime_id = self._ensure_entity(regime, "REGIME") if regime else None

        if sector_id:
            self._add_triple(ticker_id, "in_sector", sector_id)
        if outcome_id:
            self._add_triple(ticker_id, "exit_reason", outcome_id,
                             source_memory_id=trade_id)
        if regime_id:
            self._add_triple(ticker_id, "occurred_during", regime_id,
                             source_memory_id=trade_id)

        for member_name in (members or []):
            member_id = self._ensure_entity(member_name, "MEMBER")
            self._add_triple(member_id, "traded", ticker_id,
                             source_memory_id=trade_id)

        if trade_id:
            self._add_mention(ticker_id, "trade_memories", trade_id)
            if sector_id:
                self._add_mention(sector_id, "trade_memories", trade_id)

        log.info("KG: indexed %s → %s entities, %d members",
                 ticker, sector or "?", len(members or []))

    def index_thesis(self, ticker, sector, conviction, direction,
                     members=None, thesis_id=None):
        """Index a thesis (including vetoes) into the graph."""
        ticker_id = self._ensure_entity(ticker, "TICKER")
        if sector:
            sector_id = self._ensure_entity(sector, "SECTOR")
            self._add_triple(ticker_id, "in_sector", sector_id)

        conv_id = self._ensure_entity(conviction, "CONVICTION")
        self._add_triple(ticker_id, "last_conviction", conv_id,
                         source_memory_id=thesis_id)

        for member_name in (members or []):
            member_id = self._ensure_entity(member_name, "MEMBER")
            self._add_triple(member_id, "signaled", ticker_id,
                             source_memory_id=thesis_id)

    # ── Graph queries ──────────────────────────────────────────────

    def get_neighbors(self, entity_name, entity_type=None, max_hops=2):
        """Get entities connected to the given entity within max_hops."""
        conn = self._get_conn()
        cur = conn.cursor()

        if entity_type:
            cur.execute(
                "SELECT id FROM kg_entities WHERE name = %s AND entity_type = %s",
                (entity_name, entity_type),
            )
        else:
            cur.execute(
                "SELECT id FROM kg_entities WHERE name = %s", (entity_name,),
            )
        row = cur.fetchone()
        if not row:
            return []

        seed_id = row[0]
        visited = {seed_id}
        frontier = [seed_id]
        results = []

        for hop in range(max_hops):
            next_frontier = []
            for node_id in frontier:
                cur.execute("""
                    SELECT e.name, e.entity_type, t.predicate, t.weight
                    FROM kg_triples t JOIN kg_entities e ON t.object_id = e.id
                    WHERE t.subject_id = %s
                    UNION
                    SELECT e.name, e.entity_type, t.predicate, t.weight
                    FROM kg_triples t JOIN kg_entities e ON t.subject_id = e.id
                    WHERE t.object_id = %s
                """, (node_id, node_id))
                for name, etype, pred, weight in cur.fetchall():
                    cur2 = conn.cursor()
                    cur2.execute(
                        "SELECT id FROM kg_entities WHERE name=%s AND entity_type=%s",
                        (name, etype),
                    )
                    nid = cur2.fetchone()[0]
                    if nid not in visited:
                        visited.add(nid)
                        next_frontier.append(nid)
                        results.append({
                            "name": name,
                            "type": etype,
                            "predicate": pred,
                            "weight": weight,
                            "hops": hop + 1,
                        })
            frontier = next_frontier

        return results

    def ppr(self, seed_entities, alpha=0.15, max_iter=20):
        """Personalized PageRank from seed entities.

        Returns entity scores ranked by relevance to the seeds.
        Alpha is the teleport probability (how much to favor seeds).
        """
        conn = self._get_conn()
        cur = conn.cursor()

        seed_ids = []
        for name, etype in seed_entities:
            cur.execute(
                "SELECT id FROM kg_entities WHERE name = %s AND entity_type = %s",
                (name, etype),
            )
            row = cur.fetchone()
            if row:
                seed_ids.append(row[0])

        if not seed_ids:
            return []

        cur.execute("SELECT id FROM kg_entities")
        all_ids = [r[0] for r in cur.fetchall()]
        if not all_ids:
            return []

        adj = defaultdict(list)
        cur.execute("SELECT subject_id, object_id, weight FROM kg_triples")
        for s, o, w in cur.fetchall():
            adj[s].append((o, w))
            adj[o].append((s, w))

        n = len(all_ids)
        id_to_idx = {eid: i for i, eid in enumerate(all_ids)}
        scores = [0.0] * n

        teleport = [0.0] * n
        for sid in seed_ids:
            if sid in id_to_idx:
                teleport[id_to_idx[sid]] = 1.0 / len(seed_ids)

        for s in range(n):
            scores[s] = teleport[s]

        for _ in range(max_iter):
            new_scores = [0.0] * n
            for i, eid in enumerate(all_ids):
                neighbors = adj.get(eid, [])
                if neighbors:
                    total_w = sum(w for _, w in neighbors)
                    for nid, w in neighbors:
                        if nid in id_to_idx:
                            new_scores[id_to_idx[nid]] += (1 - alpha) * scores[i] * w / total_w
                new_scores[i] += alpha * teleport[i]
            scores = new_scores

        ranked = []
        for i, eid in enumerate(all_ids):
            if scores[i] > 1e-6 and eid not in [id_to_idx.get(s) for s in seed_ids]:
                cur.execute("SELECT name, entity_type FROM kg_entities WHERE id = %s", (eid,))
                row = cur.fetchone()
                if row:
                    ranked.append({"name": row[0], "type": row[1], "score": scores[i]})

        ranked.sort(key=lambda x: -x["score"])
        return ranked[:20]

    def query_for_thesis(self, ticker, sector=None, members=None):
        """High-level query: what does the graph know about this ticker?

        Returns a formatted string for injection into the thesis prompt.
        """
        lines = []

        neighbors = self.get_neighbors(ticker, "TICKER", max_hops=2)
        if not neighbors:
            return ""

        outcomes = [n for n in neighbors if n["type"] == "OUTCOME"]
        sectors = [n for n in neighbors if n["type"] == "SECTOR"]
        members_found = [n for n in neighbors if n["type"] == "MEMBER"]
        regimes = [n for n in neighbors if n["type"] == "REGIME"]

        if outcomes:
            outcome_dist = defaultdict(int)
            for o in outcomes:
                outcome_dist[o["name"]] += 1
            dist_str = ", ".join(f"{k}: {v}" for k, v in outcome_dist.items())
            lines.append(f"GRAPH: {ticker} outcome history: {dist_str}")

        if members_found:
            member_names = [m["name"] for m in members_found]
            lines.append(f"GRAPH: Congressional members who traded {ticker}: {', '.join(member_names)}")

        if sector and members:
            seeds = [(ticker, "TICKER")]
            if sector:
                seeds.append((sector, "SECTOR"))
            for m in (members or []):
                seeds.append((m, "MEMBER"))

            ppr_results = self.ppr(seeds, alpha=0.15)
            related_tickers = [r for r in ppr_results if r["type"] == "TICKER"][:3]
            if related_tickers:
                related = ", ".join(f"{r['name']} ({r['score']:.3f})" for r in related_tickers)
                lines.append(f"GRAPH: Related tickers by association: {related}")

        return "\n".join(lines)

    def stats(self):
        """Return graph statistics."""
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM kg_entities")
        entities = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM kg_triples")
        triples = cur.fetchone()[0]
        cur.execute("SELECT entity_type, COUNT(*) FROM kg_entities GROUP BY entity_type ORDER BY COUNT(*) DESC")
        by_type = {r[0]: r[1] for r in cur.fetchall()}
        return {"entities": entities, "triples": triples, "by_type": by_type}

    def close(self):
        if self._conn and not self._conn.closed:
            self._conn.close()
