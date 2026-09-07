-- Kognit: Phase 5C activation - subject-only chat evidence.
--
-- Run this once in the Supabase SQL editor, AFTER
-- 0003_learning_memory_foundation.sql has already been applied.
--
-- WHY: backend.learning_memory.resolve_academic_context() now performs
-- deterministic, zero-friction SUBJECT detection from the student's own
-- message text and recent chat history (see backend/learning_memory.py's
-- module docstring and the Phase 5C activation investigation report for
-- the full reasoning). It deliberately NEVER extracts a TOPIC from
-- freeform message text - Kognit has no reliable, finite topic
-- vocabulary, and reliably carving a topic phrase out of arbitrary
-- Bangla/English/Banglish sentences without NLP/embeddings/an LLM call is
-- not achievable with acceptable precision.
--
-- Consequence: a Known or Probable chat learning_evidence row can now
-- legitimately have subject set but topic/topic_key NULL. The original
-- 0003 migration's NOT NULL constraints on topic/topic_key made this
-- impossible - this migration loosens exactly those two columns and
-- nothing else. attribution_confidence's CHECK constraint (known/probable
-- only, for this table) is unchanged.
--
-- PURELY ADDITIVE IN EFFECT (loosens a constraint, does not drop or
-- reinterpret any existing column, does not touch quiz_attempts,
-- quiz_answers, conversation_index, or any Phase 5A object).

alter table public.learning_evidence
    alter column topic drop not null;

alter table public.learning_evidence
    alter column topic_key drop not null;