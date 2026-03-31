"""
Market Memory — Knowledge Graph Engine for the Sovereign.

Provides persistent, graph-structured memory that lets the agent learn from
past market regimes, decisions, and outcomes via Personalized PageRank retrieval.
"""

from memory.kg_engine import (
    init_db,
    add_entity,
    add_relationship,
    record_event,
    record_decision,
    record_outcome,
    record_regime_change,
    associative_query,
    get_regime_history,
    get_similar_conditions,
    get_entity_context,
)
from memory.market_context import build_market_context, enrich_from_run

__all__ = [
    # KG Engine
    "init_db",
    "add_entity",
    "add_relationship",
    "record_event",
    "record_decision",
    "record_outcome",
    "record_regime_change",
    "associative_query",
    "get_regime_history",
    "get_similar_conditions",
    "get_entity_context",
    # Market Context
    "build_market_context",
    "enrich_from_run",
]
