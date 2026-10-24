# Kloriz (repository: kognit-app)

Kloriz is an AI academic assistant for Bangladeshi secondary and higher-secondary students
(NCTB first). FastAPI backend, vanilla-JS frontend, Supabase (Postgres + Auth + RLS) and the
Gemini API behind a provider abstraction (`backend/providers/`).

This README is the quick start. Operations, configuration, migrations, limits and rollback are
in [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md). Cost and usage SQL is in
[`docs/USAGE_REPORTS.sql`](docs/USAGE_REPORTS.sql).

## Run locally

```bash
python -m venv venv && source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                    # then fill in real values (never commit .env)
uvicorn backend.main:app --reload
```

Open http://127.0.0.1:8000. For local development without a configured database, set
`QUOTA_MODE=off` in `.env` (see docs/DEPLOYMENT.md).

## Test

```bash
pip install pytest
# dummy values are enough: the suite makes no network calls
GEMINI_API_KEY=x SUPABASE_URL=https://example.invalid SUPABASE_ANON_KEY=x pytest -q
```

Optional layers:

* **Database behaviour** (quotas, concurrency, privileges, RLS, migrations on a fresh database):
  needs a throwaway PostgreSQL server. `PGHOST=<socket or host> PGUSER=<user> pytest test_database_behaviour.py`
  (skipped automatically when `PGHOST` is not set). Never point it at a real Supabase project.
* **Frontend** (jsdom): `cd tests_frontend && npm install && npm run test:jsdom`.
  The `test:browser` scripts additionally need a local Chrome/Chromium.
* **Local concurrency probe** (no external calls): `python tools/local_load_test.py`.
  It measures this application's own queueing behaviour only; read the header of that file
  before quoting any number from it.

## Layout

| Path | Purpose |
|---|---|
| `backend/main.py` | HTTP routes, auth dependencies, rate limiting, in-memory stores |
| `backend/ai_engine.py`, `backend/providers/` | prompts, retry/recovery, provider abstraction (Gemini + mock) |
| `backend/quota_service.py` | server-side plan quotas (the rules live in the database) |
| `backend/usage_collector.py`, `backend/usage_telemetry.py` | privacy-safe AI usage records |
| `backend/state_limits.py` | bounded, expiring in-memory PDF/quiz state |
| `backend/payments.py` | payment-verification boundary (no live provider yet) |
| `supabase/migrations/` | schema + RLS, apply in numeric order |
| `supabase/tests/` | PostgreSQL behaviour tests for the migrations |
| `evaluation/` | isolated answer-quality benchmark subsystem |
| `static/`, `templates/` | frontend |