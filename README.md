# REGISMEET — AI-Powered Meeting Intelligence & Event Summarizer

A multi-tenant B2B SaaS platform that ingests webinar/meeting audio, transcribes it, extracts a structured executive summary (topics, action items, deadlines) using Google Gemini, and makes every meeting searchable across an organization's entire history using natural-language semantic search over pgvector embeddings.

Built as a serverless-first architecture on Vercel + Supabase + Upstash, with zero-trust multi-tenant isolation enforced at the database layer.

---

## What it does

1. A company uploads a meeting recording (`.mp3` / `.wav`), either through the dashboard or an inbound signed webhook stream.
2. The audio is transcribed via OpenAI Whisper.
3. The transcript is sent to **Gemini 2.5 Flash** with a strict Pydantic-validated JSON schema, extracting:
   - Executive summary
   - Key topics
   - Action items (assignee, task, deadline)
   - Project deadlines
4. The summary is embedded (1536-dim) and stored in Postgres via `pgvector`.
5. Users can later run natural-language semantic search ("what did we decide about the Q3 roadmap?") across every meeting their organization has ever processed, ranked by cosine similarity.
6. Usage is metered per organization against a monthly quota, and billed through Paystack subscriptions.

---

## Architecture

```
┌──────────────┐      ┌──────────────────┐      ┌───────────────────┐
│   Frontend    │────▶│  api/index.py     │────▶│  Supabase Storage  │
│  (React/Next) │      │  (FastAPI on      │      │  (raw audio,       │
│               │◀────│   Vercel)         │      │   presigned URLs)  │
└──────────────┘      └──────────────────┘      └───────────────────┘
       ▲                       │
       │ Supabase Realtime     │ QStash (durable HTTP job queue)
       │ (live status toasts)  ▼
       │              ┌──────────────────┐
       └──────────────│ api/process_job.py│
                       │ Whisper → Gemini  │
                       │ → embeddings      │
                       └────────┬─────────┘
                                │
                                ▼
                    ┌───────────────────────┐
                    │  Supabase Postgres     │
                    │  + pgvector + RLS      │
                    │  + pg_cron             │
                    └───────────────────────┘
```

**Why serverless functions instead of one long-running server:** Vercel functions are stateless and short-lived, so the pipeline is split into a fast interactive gateway (`api/index.py`) and a separately-scaled, longer-running worker (`api/process_job.py`) invoked durably through Upstash QStash rather than a polling worker loop.

---

## Tech stack

| Layer                 | Technology                                                           |
| --------------------- | -------------------------------------------------------------------- |
| API Gateway           | FastAPI (Python 3.12), deployed as a Vercel serverless function      |
| Job queue             | Upstash QStash (durable HTTP retries, no persistent worker process)  |
| State / rate limiting | Upstash Redis (REST, serverless-safe)                                |
| Database              | Supabase Postgres + `pgvector` + `pg_cron`                           |
| Transcription         | OpenAI Whisper                                                       |
| Summarization         | Google Gemini 2.5 Flash (structured JSON output, Pydantic-validated) |
| Embeddings            | OpenAI `text-embedding-3-small` (1536-dim)                           |
| Billing               | Paystack (subscriptions, signature-verified webhooks)                |
| Email                 | Resend                                                               |
| Realtime UI updates   | Supabase Realtime broadcast                                          |
| Bot protection        | Cloudflare Turnstile                                                 |
| Frontend components   | React + Tailwind CSS, `lucide-react` icons                           |

---

## Security model

This project was built around eleven explicit zero-trust security pillars, enforced in code rather than left as configuration:

1. **Data privacy** — temporary audio files are purged from disk and Storage immediately after processing completes.
2. **Row-Level Security** — every tenant-scoped table has RLS policies keyed off `organization_id` extracted from the verified JWT.
3. **Auth hardening** — Redis-backed lockouts after repeated failures, with randomized delays to resist account enumeration.
4. **Security headers** — CSP, HSTS, X-Frame-Options, and nosniff attached via middleware on every response.
5. **Anti-BOLA** — every database lookup explicitly filters by the caller's `company_id`; no object is ever fetched by ID alone.
6. **Server-side validation** — every endpoint uses strict Pydantic V2 schemas; client-side checks are treated as cosmetic only.
7. **No credential leakage** — API responses use dedicated DTOs, never raw internal table rows.
8. **API proxy architecture** — Gemini, Whisper, Paystack, and Resend keys never reach the browser; every third-party call is proxied server-side.
9. **Rate limiting** — a Redis token-bucket limiter shields paid AI endpoints from abuse or billing spikes.
10. **Bot protection** — Cloudflare Turnstile on sensitive routes, CORS locked to explicit allowed origins.
11. **Sanitized errors** — stack traces are logged server-side only; clients receive a generic, non-identifying error message.

---

## Project structure

```
regismeet/
├── api/
│   ├── index.py            # Vercel entry point — FastAPI gateway
│   └── process_job.py      # QStash-invoked transcription/summarization worker
├── UI_Components/
│   ├── RealtimeBanner.jsx        # Live processing-status toasts (Supabase Realtime)
│   ├── WebinarSummaryRow.jsx     # Expandable per-meeting summary row
│   ├── SemanticSearchCard.jsx    # Natural-language search UI
│   ├── UsageAnalyticsCard.jsx    # Quota usage + 80% warning threshold
│   └── markdownExporter.js       # Client-side Markdown export/download
├── ai_summarizer.py         # Whisper transcription + Gemini structured summarization
├── alert_system.py          # Realtime broadcast + Resend email + Slack alerts
├── app_config.py            # Pydantic V2 settings (all environment configuration)
├── audio_webhook.py         # Upload, webhook ingestion, and semantic search routes
├── paystack_billing.py      # Checkout proxy + signed webhook handler
├── redis_queue_engine.py    # Upstash Redis state/rate-limiting + QStash job dispatch
├── file_processor.py        # Magic-number audio file validation
├── db_migration.sql         # Full schema, RLS policies, pgvector, pg_cron
├── requirements.txt
└── vercel.json
```

---

## Local setup

### 1. Environment

```bash
py -3.12 -m venv venv
venv\Scripts\activate        # Windows
pip install -r requirements.txt
```

On Windows, `python-magic` needs a Windows-compatible build:

```bash
pip uninstall python-magic -y
pip install python-magic-bin
```

### 2. Provision services

- Create a **Supabase** project, run `db_migration.sql` in the SQL Editor, and create a private Storage bucket named `webinar-audio`.
- Create an **Upstash Redis** database and an **Upstash QStash** account.
- Get API keys for **Google AI Studio (Gemini)**, **OpenAI**, **Paystack**, **Resend**, and **Cloudflare Turnstile**.

### 3. Configure environment

```bash
cp .env.example .env
# fill in every value in .env with your real credentials
```

### 4. Run locally

```bash
npm install -g vercel
vercel dev
```

---

## License

This project is provided as a portfolio/reference implementation.
