"""
Kloriz AI Engine 2.0 - orchestration layer.

Dependency direction (no cycles, nothing here imports backend.ai_engine,
backend.main or any provider SDK):

    config   <- everything (environment flags, one place)
    registry <- router, cost, gateway      (models, capabilities, versioned prices)
    health   <- router, gateway            (rolling health + circuit breaker)
    router   <- ai_engine                  (deterministic, explainable model choice)
    gateway  <- ai_engine                  (deadline, retry budget, failure policy)
    prompts  <- ai_engine                  (layered, versioned prompt blocks)
    verify   <- main                       (shadow-mode answer verification)

The attempt loop that talks to the provider still lives in
backend/ai_engine.py (it carries the Bug-3 streaming contract and its tests);
this package owns every POLICY decision that loop consults.
"""