-- Kloriz: AI usage and cost reports. READ-ONLY queries for the Supabase SQL editor
-- (run as the project owner; students cannot read public.ai_usage_events).
--
-- WHAT IS AND IS NOT KNOWN
--   * Token counts are what the provider reported. NULL means "not reported", and
--     every SUM below ignores NULLs, so a total can UNDER-state usage if many rows
--     are NULL. Query 0 shows how complete the data is - read it first.
--   * Cost needs a verified price list. None is stored in this repository, so the
--     queries take prices as parameters you type in (USD per MILLION tokens). Check
--     Google's official pricing page first; do not paste a number from a blog.
--   * This table is telemetry: a student could insert rows for their own user_id.
--     It is not a billing ledger.
--
-- Set the window and prices once, then run the queries. (In the SQL editor, replace
-- the literals in the `params` CTE of each query; kept inline so each is standalone.)

-- 0. DATA QUALITY: how complete is the usage data?
select operation, status, count(*) as rows,
       round(100.0 * count(input_tokens) / count(*), 1)  as pct_with_input_tokens,
       round(100.0 * count(output_tokens) / count(*), 1) as pct_with_output_tokens
from public.ai_usage_events
where created_at >= now() - interval '7 days'
group by 1, 2 order by 1, 2;

-- 1. COST PER REQUEST / PER DAY, by model and operation.
--    input_usd_per_m / output_usd_per_m: VERIFIED prices for that model (thinking
--    tokens are billed as output by Gemini's documentation; confirm on the official
--    page and adjust the expression if that changes).
with params as (select 0.00::numeric as input_usd_per_m, 0.00::numeric as output_usd_per_m)
select date_trunc('day', e.created_at at time zone 'Asia/Dhaka')::date as day_dhaka,
       e.model, e.operation, count(*) as calls,
       sum(e.input_tokens) as input_tokens,
       sum(coalesce(e.output_tokens, 0) + coalesce(e.thinking_tokens, 0)) as output_plus_thinking_tokens,
       round((sum(e.input_tokens) * p.input_usd_per_m
            + sum(coalesce(e.output_tokens, 0) + coalesce(e.thinking_tokens, 0)) * p.output_usd_per_m) / 1e6, 4) as est_usd,
       round((sum(e.input_tokens) * p.input_usd_per_m
            + sum(coalesce(e.output_tokens, 0) + coalesce(e.thinking_tokens, 0)) * p.output_usd_per_m) / 1e6 / nullif(count(*), 0), 6) as est_usd_per_call
from public.ai_usage_events e, params p
where e.created_at >= now() - interval '30 days'
group by 1, 2, 3, p.input_usd_per_m, p.output_usd_per_m
order by 1 desc, 2, 3;

-- 2. COST PER USER PER DAY (top spenders) - same price parameters.
with params as (select 0.00::numeric as input_usd_per_m, 0.00::numeric as output_usd_per_m)
select e.user_id, date_trunc('day', e.created_at at time zone 'Asia/Dhaka')::date as day_dhaka,
       count(*) as calls,
       round((sum(e.input_tokens) * p.input_usd_per_m
            + sum(coalesce(e.output_tokens, 0) + coalesce(e.thinking_tokens, 0)) * p.output_usd_per_m) / 1e6, 4) as est_usd
from public.ai_usage_events e, params p
where e.created_at >= now() - interval '7 days'
group by 1, 2, p.input_usd_per_m, p.output_usd_per_m
order by est_usd desc nulls last
limit 50;

-- 3. RETRY / RECOVERY OVERHEAD: how many provider calls were spent on retries?
select operation,
       count(*) filter (where attempt > 1 or recovery_used) as retry_or_recovery_calls,
       count(*) as total_calls,
       round(100.0 * count(*) filter (where attempt > 1 or recovery_used) / nullif(count(*), 0), 2) as pct_overhead
from public.ai_usage_events
where created_at >= now() - interval '7 days'
group by 1 order by 1;

-- 4. LATENCY PERCENTILES (successful calls), by operation and model.
select operation, model, count(*) as n,
       percentile_cont(0.50) within group (order by duration_ms) as p50_ms,
       percentile_cont(0.95) within group (order by duration_ms) as p95_ms,
       percentile_cont(0.99) within group (order by duration_ms) as p99_ms,
       percentile_cont(0.50) within group (order by ttft_ms)     as ttft_p50_ms,
       percentile_cont(0.95) within group (order by ttft_ms)     as ttft_p95_ms
from public.ai_usage_events
where status = 'success' and created_at >= now() - interval '7 days'
group by 1, 2 order by 1, 2;

-- 5. FAILURE RATE by operation and error code.
select operation, status, coalesce(error_code, '-') as error_code, count(*) as n,
       round(100.0 * count(*) / sum(count(*)) over (partition by operation), 2) as pct_of_operation
from public.ai_usage_events
where created_at >= now() - interval '7 days'
group by 1, 2, 3 order by 1, n desc;

-- 6. FREE vs PRO USAGE DISTRIBUTION (plan as recorded at request time).
select coalesce(plan, 'unknown') as plan, operation, count(*) as calls, count(distinct user_id) as users
from public.ai_usage_events
where created_at >= now() - interval '30 days'
group by 1, 2 order by 1, 2;

-- 7. QUOTA PRESSURE: how many students hit their limit this week? (ledger counts
--    accepted requests; refunded ones are excluded)
select kind, period_key, count(distinct user_id) as students_using, count(*) as counted_requests,
       count(*) filter (where refunded_at is not null) as refunded
from public.quota_ledger
where created_at >= now() - interval '14 days'
group by 1, 2 order by 2 desc, 1;

-- 8. RETENTION (run deliberately, not on a schedule you have not approved). The
--    retention period is a founder/legal decision; 180 days is only a starting point.
-- delete from public.ai_usage_events where created_at < now() - interval '180 days';
-- delete from public.quota_ledger    where created_at < now() - interval '180 days';