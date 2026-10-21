# BlueIntel Nigeria API

FastAPI back-end for BlueIntel Nigeria, built from the *PRD & SRS v1.0*. It ingests, validates and serves ocean/coastal data with source, unit, timestamp and freshness on every value, evaluates alert rules, and offers **Groq-powered, data-grounded AI insights**.

> **Data status:** the default source is a synthetic **demo** generator. No real provider is approved yet (PRD §12, Appendix A). `open_meteo` is included as a *candidate* adapter and stays blocked until you set `SOURCE_LICENCE_REVIEWED=true`.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
cp .env.example .env                                   # then edit it (see below)
uvicorn app.main:app --reload                          # http://localhost:8000/docs
pytest                                                 # 50 tests, no network needed
```

Minimum `.env` edits:

| Variable | Why |
|---|---|
| `GROQ_API_KEY` | Enables `/insights/query`. Get one at console.groq.com. Without it only the AI endpoint returns `503 AI_NOT_CONFIGURED`; everything else works (FR-020). |
| `ADMIN_API_KEY` | Enables `/admin/*`. Generate: `python -c "import secrets; print(secrets.token_urlsafe(32))"`. Empty = admin disabled. |
| `CORS_ORIGINS` | Your front-end origin(s). |

The first start creates the SQLite DB, seeds reference data and immediately runs one ingestion; after that the scheduler runs it every `INGEST_INTERVAL_MINUTES`.

## Architecture

```
 React front-end ──HTTP/JSON──▶  FastAPI  (app/routers)        ◀── admin (X-API-Key)
                                   │
        ┌──────────────────────────┼─────────────────────────────┐
        ▼                          ▼                             ▼
  public.py                   insights.py                    admin.py
  read API, alerts,           Groq AI (optional)             feeds, runs, rules,
  feedback                         │                         audit log
        │                          ▼
        │                 services/insights.py ── retrieve evidence ─┐
        │                          │ (guardrails)                    │
        │                          ▼                                 │
        │                   services/llm.py ──▶ Groq API             │
        ▼                                                            ▼
  services/data.py  ◀────────── services/alerts.py ─────────▶  SQL database
  (freshness, series, gaps)         ▲                          (SQLite / PostgreSQL)
                                    │ after every run                ▲
                            services/ingestion.py ── validate ───────┘
                                    ▲  retries + backoff, run log
                                    │
                            services/adapters.py ──▶ provider (demo | open_meteo)

        APScheduler (in-process) triggers ingestion on a schedule
```

Key decisions (mapped to SRS §9.2):

- **Adapters are isolated.** Only `adapters.py` knows a provider's format; it returns neutral records. `ingestion.py` validates and stores them. Swapping a source never touches the API.
- **The browser never calls providers or Groq.** Keys live only in server env vars.
- **Scheduled ingestion, not fetch-on-view.** Pages read from the database.
- **Provenance per record.** Each observation links to the ingestion run (and so the source) that produced it.
- **One freshness policy**, per-dataset limits, in `services/data.py`. API, alerts and AI all use it (NFR-004).
- **Alert status is derived at read time.** A stale feed can never show an old alert as `active` (FR-015).
- **AI is optional.** If Groq is down, unconfigured or rate-limited, only `/insights/query` fails, with a clean error.

## Folder structure

```
blueintel-api/
├── app/
│   ├── main.py          app factory, scheduler, middleware
│   ├── config.py        settings from env
│   ├── db.py            engine/session, UTC datetime type
│   ├── models.py        tables (SRS §11 entities)
│   ├── schemas.py       request/response models
│   ├── security.py      admin auth, rate limiting, audit helper
│   ├── errors.py        consistent {"error": {code, message}}
│   ├── seed.py          sources, locations, datasets, alert rules
│   ├── routers/         public.py · insights.py · admin.py
│   └── services/        adapters.py · ingestion.py · alerts.py · data.py · insights.py · llm.py
├── tests/
├── .env.example   .gitignore   requirements.txt   requirements-dev.txt   Dockerfile
```

## Endpoints (`/api/v1`)

| Method & path | Purpose | SRS |
|---|---|---|
| `GET /health` | App health **and** per-feed health, reported separately | FR-021 |
| `GET /sources` · `/locations` · `/datasets` | Metadata, licence/attribution status | 12.3 |
| `GET /locations/{id}/summary` | Latest value per indicator + freshness + warnings | FR-008 |
| `GET /locations/{id}/series?dataset=&start=&end=` | Bounded time series, each point labelled, **gaps reported** | FR-009/010 |
| `GET /locations/{id}/series.csv?dataset=&start=&end=` | CSV export; every row has time, unit, data type, source, attribution | FR-011 |
| `GET /alerts` · `GET /alert-rules` | Alerts with trigger, value, source, issue time, validity, derived status | FR-013 |
| `POST /feedback` | Optional pilot feedback (no personal data) | 12.3 |
| `POST /insights/query` | Groq-grounded Q&A with citations | FR-017–020 |
| `GET /admin/feeds`, `/ingestion-runs`, `/audit-log`, `/feedback` | Operations (key required) | FR-021/022 |
| `POST /admin/ingestion/run` | Run ingestion now | |
| `PATCH /admin/alert-rules/{id}` · `/admin/sources/{id}` | Change thresholds / switch off a faulty source; **audited** | FR-023 |
| `PATCH /admin/datasets/{key}` | Change a dataset's freshness threshold (not below its refresh interval); **audited** | FR-024 |

Timestamps are ISO 8601 UTC. Series are limited to 14 days per request.

## AI insights (Groq)

`POST /api/v1/insights/query` with `{"question": "...", "location_id": "lekki"}` (location optional).

1. **Safety gate first:** questions about whether it is safe, navigation, emergencies or guarantees get a fixed refusal pointing to official sources, with no LLM call. Fish-location prediction is declined as unsupported (FR-019).
2. **Retrieve before answering:** latest values, 24 h range, 6 h change, forecast max and alerts for the relevant location(s) are pulled from the database as numbered evidence (`[E1]`, `[E2]`, …). No evidence → "cannot answer reliably", no LLM call (FR-017).
3. **Constrained prompt:** use only the evidence, cite every fact, state data type and time, disclose stale/missing data, treat question and evidence as *untrusted* (prompt-injection defence).
4. **Output checks:** an answer with no citation, an unknown citation id, or "it is safe to…" wording is blocked (`ungrounded_blocked`).
5. **Privacy:** the log stores model, evidence count, latency and a hash of the question, never the question text.

The model is set by `GROQ_MODEL`. Check Groq's current model list, as models are retired over time. Responses are rate-limited per IP (`INSIGHTS_RATE_LIMIT_PER_MINUTE`).

## Adding a real data source

1. Review the provider's licence, attribution, coverage, limits and commercial-use terms; record them in `seed.py`.
2. Write an adapter class in `adapters.py` returning `RawRecord`s with the correct kind (`observation` only for real measurements; model output is `model_estimate`).
3. Set `ACTIVE_SOURCE=<id>` and `SOURCE_LICENCE_REVIEWED=true`.
4. Have thresholds in `seed.py` reviewed by a marine/meteorological specialist before any pilot.

## SRS coverage

Implemented: FR-006–015, FR-011 (server-side CSV), FR-017–024 (FR-024: freshness thresholds are admin-configurable; coverage area is set in `seed.py`), NFR-002/004/005/006/008 (structured logs), NFR-007 (HSTS header in production; TLS itself terminates at the proxy), NFR-011, security controls in §14.1 (secrets in env, validated input, parameterised queries via the ORM, rate limits on all public data endpoints, admin auth, no secrets in logs). `/health` is deliberately not rate-limited so monitors work.
Not implemented: FR-016 (email/SMS/push delivery, optional in the MVP), user accounts (PRD 14.2: not needed for MVP).

**Scope note:** the PRD treats AI insights (§5.4) as post-MVP unless validated. The endpoint is built and isolated (FR-020), and can be switched off with `AI_ENABLED=false`, so you can pilot without it.

## Known limits and next steps

- **Verified:** all 50 tests pass and the server was smoke-tested locally. The Groq client is tested against a mocked HTTP transport, and the Open-Meteo adapter is *not* exercised, because tests must not call external services. **Test both with your real key and the live API before relying on them.**
- **Migrations:** tables are created with `create_all`. Add Alembic before changing the schema in production.
- **Scaling:** the scheduler and rate limiter run in-process (use one worker). For several instances, run ingestion as a separate job and move the limiter to Redis.
- **Reverse proxy:** behind a proxy, configure forwarded headers so the rate limiter sees real client IPs; terminate TLS at the proxy (NFR-007).
- **Backups and monitoring:** use your database host's backups and test a restore before launch (NFR-008). Alert on `/api/v1/health` → `feeds.status != healthy`.
- **Admin auth** is a single shared key. Replace with real users/roles if several operators are needed.
- Alert thresholds are placeholders, and the Lagos coast / three indicators are assumptions pending the PRD §18 decisions.
#   N i m a s a - a p i  
 