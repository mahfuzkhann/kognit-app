"""
Kognit Phase 7B — Answer Quality & Evaluation subsystem.

DEPENDENCY DIRECTION (enforced by convention + code review, per the
approved Phase 7B architecture):

    evaluation/  -->  backend/

    NEVER:

    backend/  -->  evaluation/

Code under this package may import from `backend` (e.g. the model
adapter in a later phase calling `backend.ai_engine.generate_ai_response`).
Nothing under `backend/` may ever import from `evaluation/` - production
Kognit behavior must not depend on the evaluation framework existing at
all.

This package intentionally has ZERO import path to `backend.database` -
the module that talks to the production Supabase project. This is a
structural guarantee, not just a policy statement: real student data
(chats, quiz attempts, learning evidence) can never accidentally flow
into benchmark content, because there is no code path by which this
package can read it.
"""
