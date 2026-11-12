# Orizon Nimasa API: frontend guide

Base URL: `http://localhost:8000` in development. All paths below are under `/api/v1`.
Interactive docs with every schema: `/docs` (Swagger) and `/openapi.json`.

## Conventions

- **Time:** every timestamp is ISO 8601 UTC (e.g. `2026-10-26T09:00:00Z`). Display in WAT (UTC+1) in the UI.
  Query parameters that take a time **must include a timezone**.
- **Errors:** always `{"error": {"code": "...", "message": "..."}}`. Validation errors (422) add
  `error.details: [{"field", "problem"}]`. Branch on `code`, show `message`.
- **Rate limits:** public data endpoints 30 requests/min per IP, feedback limited too, login 10/min,
  AI 6/min. Over the limit returns `429 RATE_LIMITED`.
- **Caching:** public responses are cacheable for 30 s; admin responses are `no-store`.
- **Data types** (`data_type`): `observation`, `forecast`, `model_estimate`, `derived`. Always label them in the UI.
- **Freshness** (`status`): `fresh`, `stale`, `empty`. Never show a `stale` or `empty` value as current.
- **Gaps:** missing values are never filled in. They are reported in a separate `gaps` list.
- **CORS:** allowed origins come from the server's `CORS_ORIGINS`; the default is `http://localhost:5173`.

---

## Public (no login)

### `GET /health`
App health and upstream feed health, reported separately.
```json
{"status": "ok", "app": {"database": "ok"},
 "feeds": {"status": "healthy|degraded|unknown",
           "sources": [{"source_id": "demo", "status": "healthy|degraded",
                        "last_successful_sync": "…Z", "last_run_status": "success",
                        "stale_datasets": []}]},
 "time": "…Z"}
```

### `GET /locations`
`[{id, name, area, lat, lng}]`, for the picker and map markers.

### `GET /datasets`
`[{key, label, description, unit, source_id, refresh_minutes, stale_after_minutes, has_forecast}]`.
Use for cards, chart tabs and the legend. `kind` is not here; it is on every point and indicator (`data_type`).
`has_forecast` says whether forecast points can appear.

### `GET /sources`
`[{id, name, provider, licence_status, licence_url, attribution, documentation_url,
refresh_interval_minutes, enabled, note}]`, for the sources page. Show `attribution` wherever the data is shown.

### `GET /locations/{id}/summary`
Latest valid value per dataset for one location. Unknown id: `404 LOCATION_NOT_FOUND`.
```json
{"location": {…}, "generated_at": "…Z", "timezone_note": "…",
 "indicators": [{"dataset": "wave_height", "label": "Wave height", "unit": "m",
                 "value": 1.7, "data_type": "observation", "observed_at": "…Z",
                 "age_minutes": 12, "status": "fresh",
                 "source": {"id": "demo", "name": "…", "attribution": "…"}}],
 "active_alerts": 1,
 "warnings": ["Wave height data is stale (…). It is not shown as current."]}
```
When `status` is not `fresh`, `value` may be stale or null: show the warning, not a "current" number.

### `GET /summaries?locations=lekki,eleko`
Same objects as above, as a list, in one request. Omit `locations` for all of them.
Use for the map and landing page. Max 20 ids; an unknown id returns 404.

### `GET /locations/{id}/series?dataset=wave_height&start=…&end=…`
Chart data. `dataset` is required. Defaults: `start` = 48 h ago, `end` = now + 48 h (so forecast points are included).
Range is limited to 14 days (`422 RANGE_TOO_LARGE`).
```json
{"location_id": "lekki", "dataset": "wave_height", "label": "…", "unit": "m",
 "status": "fresh", "source_timestamp": "…Z", "expected_interval_minutes": 60,
 "source": {…},
 "points": [{"time": "…Z", "value": 1.4, "data_type": "observation"}],
 "gaps": [{"start": "…Z", "end": "…Z"}],
 "note": "Missing values are shown as gaps. Nothing is interpolated or estimated."}
```
Draw a break in the line across each `gaps` range. Style `forecast` points differently (e.g. dashed).

### `GET /series?locations=lekki,eleko&dataset=wave_height&start=…&end=…`
A list of the objects above, one per location, in the order requested. Same rules; max 20 locations.

### `GET /locations/{id}/series.csv?dataset=…&start=…&end=…`
CSV download (columns: `time_utc, location_id, dataset, value, unit, data_type, source_id, source_name, attribution`).
Link to it directly with a normal anchor tag.

### `GET /locations/{id}/series.png` · `.jpg` · `.pdf`
Same query (`dataset`, `start`, `end`) and limits as `series.csv`. Returns a rendered chart as a download (observed line solid,
forecast dashed, gaps left open, source and generation time in the footer). Use plain anchor tags next to the CSV link.

### `GET /alerts?location_id=&status=&category=&include_expired=`
- `category`: optional, one of `coastal`, `ocean_navigation`, `waterway_navigation`. Each alert also carries `category` and `zone`
  (`coastal`, `waterway` or `offshore`); `GET /locations` returns `zone` too, so you can group the map by water type.
- `location_id`: optional.
- `status`: optional, one of `active`, `expired`, `suppressed_stale`. `expired` implicitly includes expired alerts.
- `include_expired`: default `false`.
```json
[{"id": 12, "rule_id": "…", "rule_name": "High waves", "location_id": "lekki",
  "category": "ocean_navigation", "zone": "offshore",
  "dataset": "wave_height", "condition": "wave_height >= 2.5 m", "value": 2.7,
  "threshold": 2.5, "unit": "m", "data_type": "observation", "source_id": "demo",
  "source_timestamp": "…Z", "issued_at": "…Z", "valid_until": "…Z",
  "status": "active", "message": "…"}]
```
Status is computed on every request:
`active` = current, `expired` = past `valid_until`, `suppressed_stale` = the feed is stale, so present it as
"not current" rather than as an actionable alert.

### `GET /alert-rules`
`[{id, name, category, dataset, condition, suppression_hours, validity_hours, enabled, review_status}]`, for the rules table.

### `POST /feedback`
Body (all optional, but at least one answer is required; no names or contact details are collected):
```json
{"found_information": "yes_easily|yes_with_difficulty|no",
 "source_clarity": "yes|partly|no", "comments": "max 1000 chars"}
```
Returns `201 {"status": "received"}`. There is no `rating` field yet.

### `POST /insights/query` (optional AI feature)
Body `{"question": "3–500 chars", "location_id": "lekki"}` (`location_id` optional).
```json
{"answer_type": "grounded|refused_safety|refused_unsupported|no_evidence|ungrounded_blocked",
 "answer": "…[E1]", "citations": [{"id": "E1", "location", "dataset", "value", "unit",
 "data_type", "observed_at", "freshness", "source"}],
 "limitations": ["…"], "model": "…", "disclaimer": "…"}
```
Always show `disclaimer` and `limitations`. If the AI is down you get an error response; the rest of the app is unaffected,
so hide or disable the chat rather than the dashboard.

---

## Marine and developer sandbox

These public endpoints return demo data and are explicitly labelled as such. Optionally send `X-API-Key` to meter usage against a developer key.

| Endpoint | Returns |
|---|---|
| `GET /vessels` | active demo AIS vessels with latest position, SOG and COG |
| `GET /vessels/{mmsi}/track?hours=48` | `{vessel, points: [...]}` track replay |
| `GET /bathymetry?bbox=minLng,minLat,maxLng,maxLat` | depth points `{lat, lng, depth_m, survey_name, source}` |
| `GET /tides?location_id=lekki&hours=48` | predictions, high/low waters, datum and `simulated` flag |
| `GET /data-quality` | quality summary (same shape as `/admin/quality`) |
| `POST /developer/keys` | create a sandbox key (`{name, owner_email?}`); raw key returned once |
| `GET /developer/usage` | usage for the supplied `X-API-Key` |
| `GET /developer/recipes` | base URL, auth header, endpoints, cURL and JavaScript examples |

Demo AIS/bathymetry/tides must never be presented as real navigation data. Real providers are not configured in this build.

## Admin (login required)

### Authentication
`POST /admin/auth/login` with `{"email", "password", "totp_code"?}` returns
`{"access_token", "token_type": "bearer", "expires_at", "user": {id, email, role, active, mfa_enabled, created_at, last_login_at}}`.
If MFA is enabled and the code is missing the API returns `401 MFA_REQUIRED`; show a six-digit field and retry.
Send `Authorization: Bearer <access_token>` on every admin call. Sessions last 8 hours by default.
Failures are `401 INVALID_CREDENTIALS` (wrong password, locked after 5 failures, or inactive; deliberately not distinguishable).
Missing or expired token: `401 UNAUTHORISED`. Send the user back to the login page.

| Endpoint | Purpose |
|---|---|
| `GET /admin/auth/me` | current user; use it as the route guard |
| `POST /admin/auth/logout` | revoke the current token |
| `POST /admin/auth/change-password` | `{current_password, new_password (min 12)}` |
| `POST /admin/auth/password-reset/request` | `{email}`; always `202`. In non-production without SMTP it also returns `reset_token` |
| `POST /admin/auth/password-reset/confirm` | `{token, new_password}` |
| `POST /admin/auth/mfa/setup` | returns `{secret, otpauth_uri}` |
| `POST /admin/auth/mfa/enable` | `{code}` |
| `POST /admin/auth/mfa/disable` | `{password}` |

Keep the token in memory (or `sessionStorage`); do not put it in `localStorage` if you can avoid it.

### Operations
| Endpoint | Returns / does |
|---|---|
| `GET /admin/feeds` | `{feeds: [{location_id, dataset, status, limit_minutes, source_timestamp, age_minutes}], recent_runs: [...]}` |
| `GET /admin/ingestion-runs?limit=50` | `[{run_id, source_id, started_at, completed_at, status, records, rejected, error_category, error_summary}]` |
| `POST /admin/ingestion/run?source=` | start ingestion now. `409 INGESTION_IN_PROGRESS` if one is already running |
| `PATCH /admin/sources/{id}` | `{enabled: bool}`, the switch for a faulty source |
| `PATCH /admin/datasets/{key}` | `{stale_after_minutes}`; must be at least the dataset's refresh interval |
| `GET /admin/alert-rules` | `[{id, name, dataset, operator, threshold, unit, suppression_hours, validity_hours, enabled, review_status}]` |
| `PATCH /admin/alert-rules/{id}` | any of `threshold, enabled, suppression_hours (0–168], validity_hours (0–168], review_status` |
| `GET /admin/audit-log?limit=100` | `[{at, actor, action, target, before, after}]` |
| `GET /admin/feedback?limit=100` | `[{at, found_information, source_clarity, comments}]` |
| `GET/POST /admin/staff`, `PATCH /admin/staff/{id}` | list, create (`{email, password, role}`), change role, deactivate or reset password. Staff-admin only |
| `GET/POST /admin/locations`, `PATCH/DELETE /admin/locations/{id}` | location CRUD (locations with observations cannot be deleted) |
| `POST/DELETE /admin/alert-rules`, `PATCH /admin/alert-rules/{id}` | rule CRUD; deleting a rule also deletes its events |
| `GET/POST /admin/api-keys`, `PATCH /admin/api-keys/{id}`, `POST /admin/api-keys/{id}/rotate` | issue, disable, re-quota and rotate keys. The raw key appears once |
| `GET/POST /admin/notification-targets`, `PATCH/DELETE /admin/notification-targets/{id}`, `POST .../{id}/test` | email/SMS/webhook targets and test delivery |
| `GET /admin/notifications`, `POST /admin/notifications/dispatch` | outbox status and manual dispatch |
| `GET/POST /admin/jobs`, `POST /admin/jobs/run` | durable `ingestion`, `notification_dispatch`, `retention`, `quality_scan` jobs |
| `GET /admin/quality` | data-quality summary |

Run `status` values: `running`, `success`, `partial` (some records rejected), `failed`.
Server-side role checks apply; the UI guard is only a convenience.

---

## Suggested frontend flow

1. On load: `GET /locations`, `GET /datasets`, `GET /summaries` (map and cards), `GET /alerts?status=active` (ticker).
2. Location selected: `GET /locations/{id}/series?dataset=…` per chart tab. Use `GET /series?locations=…` for comparison views.
3. Every number shown needs its unit, `data_type`, source and `observed_at`. If `status` is not `fresh`, show the warning instead.
4. Poll `/summaries` and `/alerts` about once a minute at most; data changes hourly.
