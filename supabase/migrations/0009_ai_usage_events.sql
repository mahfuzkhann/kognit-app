-- Kloriz: Pre-Phase 10 remediation - privacy-safe AI usage records.
--
-- Run AFTER 0008. SAFE TO RE-RUN. Purely additive.
--
-- One row per provider operation/attempt, written by the backend (see
-- backend/usage_telemetry.py) under the STUDENT'S OWN token after the response
-- has been sent. It answers: cost per request/user/day, retry overhead,
-- latency percentiles, failure rate, Free-vs-Pro distribution.
--
-- PRIVACY: this table holds NO prompt, PDF text, answer, quiz content or free
-- text. Columns are numbers, short labels and timestamps only. Token counts are
-- NULL when the provider did not report them (never a fabricated 0).
-- estimated_cost_usd / pricing_version are NULL until a VERIFIED price list is
-- adopted; no price is hard-coded anywhere in the repository.
--
-- RLS: students may INSERT rows for themselves and nothing else. They cannot
-- read, update or delete any row (reporting is done by the founder in the SQL
-- editor or by a service_role job - see docs/USAGE_REPORTS.sql). This table is
-- TELEMETRY, not a billing or quota ledger: a student could add rows for their
-- own id, which can only distort their own analytics; quotas use
-- public.quota_ledger (0008), which students cannot write.
--
-- RETENTION: operational telemetry should not live forever. Delete rows older
-- than the retention period decided by the founder (suggested starting point:
-- 180 days; a CEO/legal decision) with the statement in docs/USAGE_REPORTS.sql.

create table if not exists public.ai_usage_events (
    id                 bigserial primary key,
    user_id            uuid not null references auth.users(id) on delete cascade,
    request_id         text,
    plan               text,
    provider           text not null default 'gemini',
    model              text,
    operation          text not null check (operation in ('chat', 'stream', 'quiz', 'title', 'notes')),
    attempt            integer check (attempt is null or attempt >= 0),
    input_tokens       integer check (input_tokens is null or input_tokens >= 0),
    output_tokens      integer check (output_tokens is null or output_tokens >= 0),
    thinking_tokens    integer check (thinking_tokens is null or thinking_tokens >= 0),
    total_tokens       integer check (total_tokens is null or total_tokens >= 0),
    duration_ms        integer check (duration_ms is null or duration_ms >= 0),
    ttft_ms            integer check (ttft_ms is null or ttft_ms >= 0),
    status             text not null check (status in ('success', 'error', 'interrupted')),
    error_code         text check (error_code is null or length(error_code) <= 64),
    retry_count        integer check (retry_count is null or retry_count >= 0),
    recovery_used      boolean not null default false,
    estimated_cost_usd numeric(12, 6),
    pricing_version    text,
    created_at         timestamptz not null default now(),

    constraint ai_usage_events_label_lengths
        check (coalesce(length(request_id), 0) <= 64
           and coalesce(length(plan), 0) <= 32
           and coalesce(length(provider), 0) <= 32
           and coalesce(length(model), 0) <= 64)
);

create index if not exists ai_usage_events_user_time_idx
    on public.ai_usage_events (user_id, created_at desc);
create index if not exists ai_usage_events_time_idx
    on public.ai_usage_events (created_at desc);
create index if not exists ai_usage_events_model_op_idx
    on public.ai_usage_events (model, operation, created_at desc);

alter table public.ai_usage_events enable row level security;

drop policy if exists ai_usage_events_insert_own on public.ai_usage_events;
create policy ai_usage_events_insert_own on public.ai_usage_events
    for insert to authenticated with check (user_id = auth.uid());

revoke select, update, delete, truncate on public.ai_usage_events from anon, authenticated;
revoke insert on public.ai_usage_events from anon;
revoke usage, select, update on sequence public.ai_usage_events_id_seq from anon;
-- authenticated needs USAGE on the sequence to insert (bigserial default).
grant usage on sequence public.ai_usage_events_id_seq to authenticated;