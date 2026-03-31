"""
Knowledge Graph Engine for the Sovereign.
==================================================================

HippoRAG 2 / CatRAG-inspired associative memory adapted for market data.

Architecture:
- Entity types: TICKER, SECTOR, EVENT, REGIME, INDICATOR, DECISION, OUTCOME
- Relationship types: belongs_to, impacts, triggered_by, made_during,
  resulted_in, correlated_with, preceded_by
- Retrieval: Personalized PageRank with recency decay + regime similarity
- Storage: PostgreSQL (pgvector) via psycopg2

Database: market_memory on cc-postgres (localhost:5434)
"""

import sys
import logging
import math
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# Ensure coalition venv packages are importable
sys.path.insert(0, "/home/asdf/.coalition/venv/lib/python3.12/site-packages")

import numpy as np
import psycopg2
import psycopg2.extras

from core.config import (
    MARKET_DB_HOST,
    MARKET_DB_PORT,
    MARKET_DB_USER,
    MARKET_DB_PASS,
    MARKET_DB_NAME,
)

logger = logging.getLogger(__name__)

psycopg2.extras.register_uuid()

# ── Constants ───────────────────────────────────────────────────────────────

ENTITY_TYPES = frozenset([
    "TICKER", "SECTOR", "EVENT", "REGIME",
    "INDICATOR", "DECISION", "OUTCOME",
])

RELATIONSHIP_TYPES = frozenset([
    "belongs_to", "impacts", "triggered_by", "made_during",
    "resulted_in", "correlated_with", "preceded_by",
])

EMBEDDING_MODEL_NAME = "all-mpnet-base-v2"
EMBEDDING_DIM = 768

# ── Lazy embedding model ────────────────────────────────────────────────────

_embed_model = None


def _get_embed_model():
    """Lazy-load sentence-transformers model."""
    global _embed_model
    if _embed_model is None:
        from sentence_transformers import SentenceTransformer
        _embed_model = SentenceTransformer(EMBEDDING_MODEL_NAME)
        logger.info("Loaded embedding model: %s", EMBEDDING_MODEL_NAME)
    return _embed_model


def _embed(text: str) -> List[float]:
    """Embed a single text string. Returns 768-dim normalized vector."""
    model = _get_embed_model()
    vec = model.encode(text, normalize_embeddings=True)
    return vec.tolist()


def _embed_batch(texts: List[str]) -> List[List[float]]:
    """Batch embed. Returns list of 768-dim normalized vectors."""
    if not texts:
        return []
    model = _get_embed_model()
    vecs = model.encode(texts, normalize_embeddings=True)
    return vecs.tolist()


def _vec_literal(embedding: List[float]) -> str:
    """Format embedding as a pgvector literal string."""
    return "[" + ",".join(str(x) for x in embedding) + "]"


# ── Database connection ─────────────────────────────────────────────────────

_conn = None


def _get_conn():
    """Get or create a connection to market_memory database."""
    global _conn
    if _conn is None or _conn.closed:
        _conn = psycopg2.connect(
            host=MARKET_DB_HOST,
            port=MARKET_DB_PORT,
            user=MARKET_DB_USER,
            password=MARKET_DB_PASS,
            dbname=MARKET_DB_NAME,
        )
        _conn.autocommit = True
    return _conn


def _reset_conn():
    """Reset the cached connection (e.g. after creating the database)."""
    global _conn
    if _conn is not None:
        try:
            _conn.close()
        except Exception:
            pass
    _conn = None


# ── Database initialization ─────────────────────────────────────────────────

def init_db() -> Dict[str, Any]:
    """Create the market_memory database, role, and all tables.

    Safe to call repeatedly — all operations are idempotent.
    Connects to the default 'postgres' database first to create the role
    and database, then connects to market_memory for table creation.
    """
    # Step 1: Connect to 'postgres' as superuser cc to bootstrap
    bootstrap_conn = psycopg2.connect(
        host=MARKET_DB_HOST,
        port=MARKET_DB_PORT,
        user="cc",
        password="cc_resistance_2026",
        dbname="postgres",
    )
    bootstrap_conn.autocommit = True
    cur = bootstrap_conn.cursor()

    # Create role if not exists
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (MARKET_DB_USER,))
    if cur.fetchone() is None:
        cur.execute(
            "CREATE ROLE %s WITH LOGIN PASSWORD %%s" % MARKET_DB_USER,
            (MARKET_DB_PASS,),
        )
        logger.info("Created database role: %s", MARKET_DB_USER)

    # Create database if not exists
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (MARKET_DB_NAME,))
    if cur.fetchone() is None:
        cur.execute("CREATE DATABASE %s OWNER %s" % (MARKET_DB_NAME, MARKET_DB_USER))
        logger.info("Created database: %s", MARKET_DB_NAME)

    cur.close()
    bootstrap_conn.close()

    # Step 2: Connect to the new database as superuser to enable extension
    ext_conn = psycopg2.connect(
        host=MARKET_DB_HOST,
        port=MARKET_DB_PORT,
        user="cc",
        password="cc_resistance_2026",
        dbname=MARKET_DB_NAME,
    )
    ext_conn.autocommit = True
    ext_cur = ext_conn.cursor()
    ext_cur.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # Grant necessary permissions to the market user
    ext_cur.execute("GRANT ALL PRIVILEGES ON DATABASE %s TO %s" % (MARKET_DB_NAME, MARKET_DB_USER))
    ext_cur.execute("GRANT ALL ON SCHEMA public TO %s" % MARKET_DB_USER)
    ext_cur.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO %s" % MARKET_DB_USER)
    ext_cur.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO %s" % MARKET_DB_USER)
    ext_cur.close()
    ext_conn.close()

    # Step 3: Connect as market user and create tables
    _reset_conn()
    conn = _get_conn()
    cur = conn.cursor()

    # kg_entities
    cur.execute("""
        CREATE TABLE IF NOT EXISTS kg_entities (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name            TEXT NOT NULL,
            entity_type     TEXT NOT NULL,
            properties      JSONB DEFAULT '{}',
            embedding       vector(768),
            first_seen      TIMESTAMPTZ DEFAULT now(),
            last_seen       TIMESTAMPTZ DEFAULT now(),
            mention_count   INTEGER DEFAULT 1,
            UNIQUE (name, entity_type)
        )
    """)

    # kg_relationships
    cur.execute("""
        CREATE TABLE IF NOT EXISTS kg_relationships (
            id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            source_id         UUID NOT NULL REFERENCES kg_entities(id) ON DELETE CASCADE,
            target_id         UUID NOT NULL REFERENCES kg_entities(id) ON DELETE CASCADE,
            relationship_type TEXT NOT NULL,
            weight            FLOAT DEFAULT 1.0,
            properties        JSONB DEFAULT '{}',
            created_at        TIMESTAMPTZ DEFAULT now(),
            last_updated      TIMESTAMPTZ DEFAULT now(),
            UNIQUE (source_id, target_id, relationship_type)
        )
    """)

    # kg_events
    cur.execute("""
        CREATE TABLE IF NOT EXISTS kg_events (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            event_text      TEXT NOT NULL,
            event_type      TEXT NOT NULL,
            timestamp       TIMESTAMPTZ DEFAULT now(),
            entities        TEXT[],
            impact_score    FLOAT DEFAULT 0.0,
            regime_at_time  VARCHAR(50),
            properties      JSONB DEFAULT '{}',
            embedding       vector(768)
        )
    """)

    # market_regimes
    cur.execute("""
        CREATE TABLE IF NOT EXISTS market_regimes (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            regime      VARCHAR(50) NOT NULL,
            risk_score  FLOAT DEFAULT 0.0,
            started_at  TIMESTAMPTZ DEFAULT now(),
            ended_at    TIMESTAMPTZ,
            indicators  JSONB DEFAULT '{}'
        )
    """)

    # agent_decisions
    cur.execute("""
        CREATE TABLE IF NOT EXISTS agent_decisions (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            session_id      TEXT NOT NULL,
            phase           TEXT NOT NULL,
            action          TEXT NOT NULL,
            tickers         TEXT[],
            reasoning       TEXT,
            outcome         JSONB,
            regime_at_time  VARCHAR(50),
            created_at      TIMESTAMPTZ DEFAULT now(),
            embedding       vector(768)
        )
    """)

    # Indexes for fast retrieval
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_entities_type ON kg_entities(entity_type)
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_entities_name ON kg_entities(name)
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_relationships_source ON kg_relationships(source_id)
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_relationships_target ON kg_relationships(target_id)
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_relationships_type ON kg_relationships(relationship_type)
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_events_type ON kg_events(event_type)
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_events_timestamp ON kg_events(timestamp)
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_events_regime ON kg_events(regime_at_time)
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_regimes_regime ON market_regimes(regime)
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_regimes_started ON market_regimes(started_at)
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_decisions_session ON agent_decisions(session_id)
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_decisions_phase ON agent_decisions(phase)
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_decisions_created ON agent_decisions(created_at)
    """)

    # HNSW indexes for vector similarity search
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_entities_embedding
        ON kg_entities USING hnsw (embedding vector_cosine_ops)
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_events_embedding
        ON kg_events USING hnsw (embedding vector_cosine_ops)
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_decisions_embedding
        ON agent_decisions USING hnsw (embedding vector_cosine_ops)
    """)

    cur.close()

    # Verify
    cur = conn.cursor()
    cur.execute("""
        SELECT tablename FROM pg_tables
        WHERE schemaname = 'public'
        ORDER BY tablename
    """)
    tables = [r[0] for r in cur.fetchall()]
    cur.close()

    logger.info("market_memory initialized. Tables: %s", tables)
    return {"success": True, "tables": tables}


# ── Entity operations ───────────────────────────────────────────────────────

def add_entity(
    name: str,
    entity_type: str,
    properties: Optional[Dict] = None,
) -> Dict[str, Any]:
    """Upsert an entity in the knowledge graph.

    If the entity already exists (same name + type), increment mention_count
    and update last_seen. Otherwise, create it with an embedding.

    Returns: {"entity_id": str, "created": bool, "mention_count": int}
    """
    if entity_type not in ENTITY_TYPES:
        return {"error": f"Invalid entity_type: {entity_type}. Must be one of {sorted(ENTITY_TYPES)}"}

    props = properties or {}
    conn = _get_conn()
    cur = conn.cursor()

    # Try upsert
    cur.execute("""
        INSERT INTO kg_entities (name, entity_type, properties)
        VALUES (%s, %s, %s)
        ON CONFLICT (name, entity_type) DO UPDATE
            SET mention_count = kg_entities.mention_count + 1,
                last_seen = now(),
                properties = kg_entities.properties || %s
        RETURNING id, mention_count
    """, (name, entity_type, psycopg2.extras.Json(props), psycopg2.extras.Json(props)))

    row = cur.fetchone()
    entity_id = str(row[0])
    mention_count = row[1]
    created = mention_count == 1

    # Generate embedding for new entities
    if created:
        try:
            embed_text = f"{entity_type}: {name}"
            if props:
                embed_text += " | " + " ".join(f"{k}={v}" for k, v in props.items())
            emb = _embed(embed_text)
            cur.execute(
                "UPDATE kg_entities SET embedding = %s::vector WHERE id = %s",
                (_vec_literal(emb), entity_id),
            )
        except Exception as e:
            logger.warning("Failed to embed entity %s: %s", name, e)

    cur.close()
    return {"entity_id": entity_id, "created": created, "mention_count": mention_count}


def _get_or_create_entity_id(name: str, entity_type: str) -> Optional[str]:
    """Get entity ID by name+type, creating if necessary. Returns UUID string."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id FROM kg_entities WHERE name = %s AND entity_type = %s",
        (name, entity_type),
    )
    row = cur.fetchone()
    cur.close()

    if row:
        return str(row[0])

    result = add_entity(name, entity_type)
    return result.get("entity_id")


# ── Relationship operations ─────────────────────────────────────────────────

def add_relationship(
    source: str,
    target: str,
    rel_type: str,
    weight: float = 1.0,
    properties: Optional[Dict] = None,
    source_type: str = "TICKER",
    target_type: str = "SECTOR",
) -> Dict[str, Any]:
    """Create or strengthen a relationship between two entities.

    Source and target are entity names. They must already exist or will be
    created with the given types. If the relationship exists, weight is
    accumulated and last_updated is refreshed.

    Returns: {"relationship_id": str, "created": bool, "weight": float}
    """
    if rel_type not in RELATIONSHIP_TYPES:
        return {"error": f"Invalid rel_type: {rel_type}. Must be one of {sorted(RELATIONSHIP_TYPES)}"}

    source_id = _get_or_create_entity_id(source, source_type)
    target_id = _get_or_create_entity_id(target, target_type)

    if not source_id or not target_id:
        return {"error": "Failed to resolve entity IDs"}

    props = properties or {}
    conn = _get_conn()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO kg_relationships (source_id, target_id, relationship_type, weight, properties)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (source_id, target_id, relationship_type) DO UPDATE
            SET weight = kg_relationships.weight + %s,
                last_updated = now(),
                properties = kg_relationships.properties || %s
        RETURNING id, weight
    """, (
        source_id, target_id, rel_type, weight, psycopg2.extras.Json(props),
        weight, psycopg2.extras.Json(props),
    ))

    row = cur.fetchone()
    rel_id = str(row[0])
    final_weight = row[1]
    cur.close()

    return {"relationship_id": rel_id, "created": final_weight == weight, "weight": final_weight}


# ── Event recording ─────────────────────────────────────────────────────────

def record_event(
    event_text: str,
    event_type: str,
    entities: Optional[List[str]] = None,
    impact_score: float = 0.0,
    regime: Optional[str] = None,
    properties: Optional[Dict] = None,
) -> Dict[str, Any]:
    """Store a market event with its embedding.

    Also upserts any named entities and links them to the event via
    'triggered_by' relationships.

    Returns: {"event_id": str}
    """
    ent_list = entities or []
    props = properties or {}

    try:
        emb = _embed(event_text)
    except Exception as e:
        logger.warning("Failed to embed event: %s", e)
        emb = None

    conn = _get_conn()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO kg_events (event_text, event_type, entities, impact_score,
                               regime_at_time, properties, embedding)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (
        event_text,
        event_type,
        ent_list,
        impact_score,
        regime,
        psycopg2.extras.Json(props),
        _vec_literal(emb) if emb else None,
    ))

    event_id = str(cur.fetchone()[0])

    # Create entity nodes for each mentioned entity and link to the event entity
    event_entity = add_entity(event_text[:120], "EVENT", {"event_id": event_id, "event_type": event_type})
    for ent_name in ent_list:
        # Auto-detect type: if it looks like a ticker (all caps, short), use TICKER
        if ent_name.isupper() and len(ent_name) <= 5:
            ent_type = "TICKER"
        else:
            ent_type = "EVENT"
        add_entity(ent_name, ent_type)
        add_relationship(
            ent_name, event_text[:120], "triggered_by",
            weight=impact_score or 1.0,
            source_type=ent_type, target_type="EVENT",
        )

    cur.close()
    return {"event_id": event_id}


# ── Decision recording ──────────────────────────────────────────────────────

def record_decision(
    session_id: str,
    phase: str,
    action: str,
    tickers: Optional[List[str]] = None,
    reasoning: Optional[str] = None,
    regime: Optional[str] = None,
) -> Dict[str, Any]:
    """Store an agent decision with its embedding.

    Returns: {"decision_id": str}
    """
    tickers_list = tickers or []

    embed_text = f"Phase: {phase} | Action: {action}"
    if tickers_list:
        embed_text += f" | Tickers: {','.join(tickers_list)}"
    if reasoning:
        embed_text += f" | {reasoning[:300]}"

    try:
        emb = _embed(embed_text)
    except Exception as e:
        logger.warning("Failed to embed decision: %s", e)
        emb = None

    conn = _get_conn()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO agent_decisions (session_id, phase, action, tickers, reasoning,
                                     regime_at_time, embedding)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (
        session_id, phase, action, tickers_list, reasoning,
        regime, _vec_literal(emb) if emb else None,
    ))

    decision_id = str(cur.fetchone()[0])
    cur.close()

    # Create entity nodes for tickers and link to decision
    decision_entity = add_entity(
        f"decision:{decision_id[:8]}", "DECISION",
        {"session_id": session_id, "phase": phase, "action": action},
    )
    if regime:
        add_entity(regime, "REGIME")
        add_relationship(
            f"decision:{decision_id[:8]}", regime, "made_during",
            source_type="DECISION", target_type="REGIME",
        )
    for ticker in tickers_list:
        add_entity(ticker, "TICKER")
        add_relationship(
            f"decision:{decision_id[:8]}", ticker, "impacts",
            source_type="DECISION", target_type="TICKER",
        )

    return {"decision_id": decision_id}


def record_outcome(
    decision_id: str,
    outcome_data: Dict[str, Any],
) -> Dict[str, Any]:
    """Link an outcome to a previously recorded decision.

    Returns: {"decision_id": str, "updated": bool}
    """
    conn = _get_conn()
    cur = conn.cursor()

    cur.execute("""
        UPDATE agent_decisions
        SET outcome = %s
        WHERE id = %s
        RETURNING id, tickers, phase, action
    """, (psycopg2.extras.Json(outcome_data), decision_id))

    row = cur.fetchone()
    if not row:
        cur.close()
        return {"decision_id": decision_id, "updated": False, "error": "Decision not found"}

    _, tickers, phase, action = row

    # Create OUTCOME entity and link
    outcome_summary = outcome_data.get("summary", str(outcome_data)[:120])
    outcome_entity = add_entity(
        f"outcome:{decision_id[:8]}", "OUTCOME",
        outcome_data,
    )
    add_relationship(
        f"decision:{decision_id[:8]}", f"outcome:{decision_id[:8]}", "resulted_in",
        source_type="DECISION", target_type="OUTCOME",
    )

    cur.close()
    return {"decision_id": decision_id, "updated": True}


# ── Regime tracking ─────────────────────────────────────────────────────────

def record_regime_change(
    new_regime: str,
    risk_score: float = 0.0,
    indicators: Optional[Dict] = None,
) -> Dict[str, Any]:
    """Track a regime transition.

    Closes the previous open regime (sets ended_at) and opens a new one.

    Returns: {"regime_id": str, "previous_regime": str|None}
    """
    ind = indicators or {}
    conn = _get_conn()
    cur = conn.cursor()

    # Close previous open regime
    cur.execute("""
        UPDATE market_regimes
        SET ended_at = now()
        WHERE ended_at IS NULL
        RETURNING regime
    """)
    prev_row = cur.fetchone()
    previous_regime = prev_row[0] if prev_row else None

    # Open new regime
    cur.execute("""
        INSERT INTO market_regimes (regime, risk_score, indicators)
        VALUES (%s, %s, %s)
        RETURNING id
    """, (new_regime, risk_score, psycopg2.extras.Json(ind)))

    regime_id = str(cur.fetchone()[0])
    cur.close()

    # Create REGIME entity and link to predecessor
    add_entity(new_regime, "REGIME", {"risk_score": risk_score})
    if previous_regime and previous_regime != new_regime:
        add_relationship(
            new_regime, previous_regime, "preceded_by",
            source_type="REGIME", target_type="REGIME",
        )

    # Create indicator entities
    for ind_name, ind_val in ind.items():
        add_entity(ind_name, "INDICATOR", {"value": ind_val})
        add_relationship(
            ind_name, new_regime, "impacts",
            weight=1.0,
            source_type="INDICATOR", target_type="REGIME",
        )

    logger.info("Regime change: %s -> %s (risk=%.2f)", previous_regime, new_regime, risk_score)
    return {"regime_id": regime_id, "previous_regime": previous_regime}


# ── Personalized PageRank ───────────────────────────────────────────────────

def _personalized_pagerank(
    seed_entity_ids: List[str],
    teleport: float = 0.15,
    max_iter: int = 50,
    tol: float = 1e-6,
    query_embedding: Optional[List[float]] = None,
    current_regime: Optional[str] = None,
) -> Dict[str, float]:
    """Run Personalized PageRank from seed entities.

    Edge weighting:
    - Base weight from kg_relationships.weight
    - Recency decay: exp(-lambda * days_since_update), lambda=0.05
    - Regime similarity boost: 2x if the relationship was created during
      the same regime as current_regime
    - CatRAG-style query-adaptive weighting when query_embedding is provided

    Returns: {entity_id_str: ppr_score}
    """
    conn = _get_conn()
    cur = conn.cursor()

    # Load graph edges with metadata
    cur.execute("""
        SELECT source_id::text, target_id::text, weight, last_updated,
               properties->>'regime' AS edge_regime
        FROM kg_relationships
    """)
    edges = cur.fetchall()

    if not edges:
        cur.close()
        return {}

    now = datetime.now(timezone.utc)
    neighbors = defaultdict(list)  # node -> [(neighbor, adjusted_weight)]
    all_nodes = set()

    for src, tgt, weight, last_updated, edge_regime in edges:
        base_w = weight or 1.0

        # Recency decay: exponential with half-life ~14 days
        if last_updated:
            days_old = (now - last_updated).total_seconds() / 86400.0
        else:
            days_old = 30.0  # default for unknown age
        recency_factor = math.exp(-0.05 * days_old)

        # Regime similarity boost
        regime_factor = 1.0
        if current_regime and edge_regime and edge_regime == current_regime:
            regime_factor = 2.0

        adjusted = base_w * recency_factor * regime_factor

        # Bidirectional edges
        neighbors[src].append((tgt, adjusted))
        neighbors[tgt].append((src, adjusted))
        all_nodes.add(src)
        all_nodes.add(tgt)

    # CatRAG query-adaptive edge weighting
    if query_embedding is not None:
        _apply_query_adaptive_weights(cur, neighbors, query_embedding, all_nodes)

    cur.close()

    if not all_nodes:
        return {}

    # Build index
    nodes = list(all_nodes)
    node_idx = {nid: i for i, nid in enumerate(nodes)}
    n = len(nodes)

    # Personalization vector: uniform over seed nodes
    personalization = np.zeros(n)
    valid_seeds = [sid for sid in seed_entity_ids if sid in node_idx]
    if not valid_seeds:
        return {}
    for sid in valid_seeds:
        personalization[node_idx[sid]] = 1.0 / len(valid_seeds)

    # Iterative power method
    scores = personalization.copy()
    for _ in range(max_iter):
        new_scores = np.zeros(n)
        for i, node in enumerate(nodes):
            if node in neighbors:
                total_w = sum(w for _, w in neighbors[node])
                if total_w > 0:
                    for nbr, w in neighbors[node]:
                        j = node_idx.get(nbr)
                        if j is not None:
                            new_scores[j] += (1.0 - teleport) * scores[i] * (w / total_w)

        new_scores += teleport * personalization

        if np.sum(np.abs(new_scores - scores)) < tol:
            break
        scores = new_scores

    return {nodes[i]: float(scores[i]) for i in range(n) if scores[i] > 1e-10}


def _apply_query_adaptive_weights(cur, neighbors, query_embedding, all_nodes):
    """CatRAG-inspired: amplify edges toward query-relevant entities."""
    qvec = np.array(query_embedding)

    entity_ids = list(all_nodes)
    if not entity_ids:
        return

    placeholders = ",".join(["%s"] * len(entity_ids))
    cur.execute(f"""
        SELECT id::text, embedding
        FROM kg_entities
        WHERE id::text IN ({placeholders}) AND embedding IS NOT NULL
    """, entity_ids)

    entity_sims = {}
    for eid, emb_bytes in cur.fetchall():
        if emb_bytes:
            try:
                evec = np.array(emb_bytes)
                dot = np.dot(qvec, evec)
                norm_product = np.linalg.norm(qvec) * np.linalg.norm(evec) + 1e-8
                sim = float(dot / norm_product)
                entity_sims[eid] = max(0.1, sim)
            except Exception:
                entity_sims[eid] = 0.5

    # Modulate edge weights by neighbor relevance
    for node in list(neighbors.keys()):
        new_edges = []
        for nbr, w in neighbors[node]:
            sim = entity_sims.get(nbr, 0.5)
            adaptive_w = w * (0.5 + sim)
            new_edges.append((nbr, adaptive_w))
        neighbors[node] = new_edges


# ── Associative query ───────────────────────────────────────────────────────

def associative_query(
    query_text: str,
    n_results: int = 5,
    current_regime: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Personalized PageRank retrieval for market memory.

    Pipeline:
    1. Embed the query
    2. Find seed entities via embedding similarity
    3. Run PPR with recency decay + regime similarity + query-adaptive weights
    4. Return top-N entities, decisions, and events by PPR score

    Returns list of dicts with type, content, score, and metadata.
    """
    query_emb = _embed(query_text)
    vec_lit = _vec_literal(query_emb)

    conn = _get_conn()
    cur = conn.cursor()

    # Find seed entities by embedding similarity
    cur.execute("""
        SELECT id::text, name, entity_type,
               embedding <=> %s::vector AS dist
        FROM kg_entities
        WHERE embedding IS NOT NULL
        ORDER BY dist ASC
        LIMIT 10
    """, (vec_lit,))
    seed_rows = cur.fetchall()
    seed_ids = [r[0] for r in seed_rows]

    if not seed_ids:
        cur.close()
        return []

    # Run PPR
    ppr_scores = _personalized_pagerank(
        seed_ids,
        query_embedding=query_emb,
        current_regime=current_regime,
    )

    if not ppr_scores:
        cur.close()
        return []

    # Rank entities by PPR score
    top_entities = sorted(ppr_scores.items(), key=lambda x: x[1], reverse=True)[:30]
    top_ids = [eid for eid, _ in top_entities]

    results = []

    # Fetch entity details for top-scored nodes
    placeholders = ",".join(["%s"] * len(top_ids))
    cur.execute(f"""
        SELECT id::text, name, entity_type, properties, mention_count, last_seen
        FROM kg_entities
        WHERE id::text IN ({placeholders})
    """, top_ids)

    for eid, name, etype, props, mentions, last_seen in cur.fetchall():
        score = ppr_scores.get(eid, 0)
        results.append({
            "type": "entity",
            "entity_type": etype,
            "name": name,
            "properties": props or {},
            "mention_count": mentions,
            "last_seen": last_seen.isoformat() if last_seen else None,
            "score": round(score, 8),
        })

    # Also find related decisions via embedding similarity
    cur.execute("""
        SELECT id::text, session_id, phase, action, tickers, reasoning,
               outcome, regime_at_time, created_at,
               embedding <=> %s::vector AS dist
        FROM agent_decisions
        WHERE embedding IS NOT NULL
        ORDER BY dist ASC
        LIMIT %s
    """, (vec_lit, n_results))

    for row in cur.fetchall():
        did, session, phase, action, tickers, reasoning, outcome, regime, created, dist = row
        results.append({
            "type": "decision",
            "decision_id": did,
            "session_id": session,
            "phase": phase,
            "action": action,
            "tickers": tickers or [],
            "reasoning": (reasoning or "")[:300],
            "outcome": outcome,
            "regime_at_time": regime,
            "created_at": created.isoformat() if created else None,
            "score": round(1.0 / (1.0 + dist), 8),  # Convert distance to similarity score
        })

    # Also find related events
    cur.execute("""
        SELECT id::text, event_text, event_type, timestamp, entities,
               impact_score, regime_at_time,
               embedding <=> %s::vector AS dist
        FROM kg_events
        WHERE embedding IS NOT NULL
        ORDER BY dist ASC
        LIMIT %s
    """, (vec_lit, n_results))

    for row in cur.fetchall():
        evid, text, etype, ts, ents, impact, regime, dist = row
        results.append({
            "type": "event",
            "event_id": evid,
            "event_text": text[:300],
            "event_type": etype,
            "timestamp": ts.isoformat() if ts else None,
            "entities": ents or [],
            "impact_score": impact,
            "regime_at_time": regime,
            "score": round(1.0 / (1.0 + dist), 8),
        })

    # Sort everything by score descending, return top N
    results.sort(key=lambda x: x["score"], reverse=True)
    cur.close()
    return results[:n_results]


# ── Regime history ──────────────────────────────────────────────────────────

def get_regime_history(
    regime_filter: Optional[str] = None,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Get past regime periods, optionally filtered by regime name.

    Returns list of regime records with duration info.
    """
    conn = _get_conn()
    cur = conn.cursor()

    if regime_filter:
        cur.execute("""
            SELECT id::text, regime, risk_score, started_at, ended_at, indicators
            FROM market_regimes
            WHERE regime = %s
            ORDER BY started_at DESC
            LIMIT %s
        """, (regime_filter, limit))
    else:
        cur.execute("""
            SELECT id::text, regime, risk_score, started_at, ended_at, indicators
            FROM market_regimes
            ORDER BY started_at DESC
            LIMIT %s
        """, (limit,))

    results = []
    for rid, regime, risk, started, ended, indicators in cur.fetchall():
        duration = None
        if started and ended:
            duration = (ended - started).total_seconds() / 3600.0  # hours

        results.append({
            "regime_id": rid,
            "regime": regime,
            "risk_score": risk,
            "started_at": started.isoformat() if started else None,
            "ended_at": ended.isoformat() if ended else None,
            "duration_hours": round(duration, 2) if duration else None,
            "indicators": indicators or {},
        })

    cur.close()
    return results


# ── Similar conditions ──────────────────────────────────────────────────────

def get_similar_conditions(
    current_indicators: Dict[str, float],
    n: int = 5,
) -> List[Dict[str, Any]]:
    """Find historically similar market conditions by comparing indicator JSONB.

    Uses Euclidean distance across shared indicator keys.

    Returns list of past regime records with similarity scores.
    """
    conn = _get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT id::text, regime, risk_score, started_at, ended_at, indicators
        FROM market_regimes
        WHERE indicators IS NOT NULL AND indicators != '{}'::jsonb
        ORDER BY started_at DESC
        LIMIT 200
    """)

    candidates = []
    current_keys = set(current_indicators.keys())

    for rid, regime, risk, started, ended, indicators in cur.fetchall():
        if not indicators:
            continue

        # Compute Euclidean distance across shared keys
        shared_keys = current_keys & set(indicators.keys())
        if not shared_keys:
            continue

        sq_sum = 0.0
        for k in shared_keys:
            try:
                diff = float(current_indicators[k]) - float(indicators[k])
                sq_sum += diff * diff
            except (ValueError, TypeError):
                continue

        if sq_sum == 0 and not shared_keys:
            continue

        distance = math.sqrt(sq_sum) if sq_sum > 0 else 0.0
        # Convert distance to similarity: 1 / (1 + dist)
        similarity = 1.0 / (1.0 + distance)

        duration = None
        if started and ended:
            duration = (ended - started).total_seconds() / 3600.0

        candidates.append({
            "regime_id": rid,
            "regime": regime,
            "risk_score": risk,
            "started_at": started.isoformat() if started else None,
            "ended_at": ended.isoformat() if ended else None,
            "duration_hours": round(duration, 2) if duration else None,
            "indicators": indicators,
            "similarity": round(similarity, 6),
            "shared_indicators": len(shared_keys),
        })

    cur.close()

    # Sort by similarity descending
    candidates.sort(key=lambda x: x["similarity"], reverse=True)
    return candidates[:n]


# ── Entity context ──────────────────────────────────────────────────────────

def get_entity_context(entity_name: str) -> Dict[str, Any]:
    """Get all relationships and events for a named entity.

    Returns the entity's full neighborhood: related entities, events
    that mention it, and decisions that involve it.
    """
    conn = _get_conn()
    cur = conn.cursor()

    # Find entity
    cur.execute("""
        SELECT id::text, name, entity_type, properties, mention_count,
               first_seen, last_seen
        FROM kg_entities
        WHERE name = %s
        LIMIT 1
    """, (entity_name,))
    entity_row = cur.fetchone()

    if not entity_row:
        cur.close()
        return {"error": f"Entity not found: {entity_name}"}

    eid, name, etype, props, mentions, first_seen, last_seen = entity_row

    # Outgoing relationships
    cur.execute("""
        SELECT r.relationship_type, r.weight, e.name, e.entity_type, r.last_updated
        FROM kg_relationships r
        JOIN kg_entities e ON r.target_id = e.id
        WHERE r.source_id = %s
        ORDER BY r.weight DESC
        LIMIT 50
    """, (eid,))
    outgoing = [
        {"rel_type": rt, "weight": w, "target": tn, "target_type": tt,
         "last_updated": lu.isoformat() if lu else None}
        for rt, w, tn, tt, lu in cur.fetchall()
    ]

    # Incoming relationships
    cur.execute("""
        SELECT r.relationship_type, r.weight, e.name, e.entity_type, r.last_updated
        FROM kg_relationships r
        JOIN kg_entities e ON r.source_id = e.id
        WHERE r.target_id = %s
        ORDER BY r.weight DESC
        LIMIT 50
    """, (eid,))
    incoming = [
        {"rel_type": rt, "weight": w, "source": sn, "source_type": st,
         "last_updated": lu.isoformat() if lu else None}
        for rt, w, sn, st, lu in cur.fetchall()
    ]

    # Events mentioning this entity
    cur.execute("""
        SELECT id::text, event_text, event_type, timestamp, impact_score, regime_at_time
        FROM kg_events
        WHERE %s = ANY(entities)
        ORDER BY timestamp DESC
        LIMIT 20
    """, (entity_name,))
    events = [
        {"event_id": evid, "event_text": et[:200], "event_type": evt,
         "timestamp": ts.isoformat() if ts else None,
         "impact_score": imp, "regime_at_time": reg}
        for evid, et, evt, ts, imp, reg in cur.fetchall()
    ]

    # Decisions involving this entity (search in tickers array)
    cur.execute("""
        SELECT id::text, session_id, phase, action, tickers, reasoning,
               outcome, regime_at_time, created_at
        FROM agent_decisions
        WHERE %s = ANY(tickers)
        ORDER BY created_at DESC
        LIMIT 20
    """, (entity_name,))
    decisions = [
        {"decision_id": did, "session_id": sid, "phase": ph, "action": act,
         "tickers": t or [], "reasoning": (reas or "")[:200],
         "outcome": out, "regime_at_time": reg,
         "created_at": ca.isoformat() if ca else None}
        for did, sid, ph, act, t, reas, out, reg, ca in cur.fetchall()
    ]

    cur.close()

    return {
        "entity": {
            "id": eid,
            "name": name,
            "entity_type": etype,
            "properties": props or {},
            "mention_count": mentions,
            "first_seen": first_seen.isoformat() if first_seen else None,
            "last_seen": last_seen.isoformat() if last_seen else None,
        },
        "outgoing_relationships": outgoing,
        "incoming_relationships": incoming,
        "events": events,
        "decisions": decisions,
    }
