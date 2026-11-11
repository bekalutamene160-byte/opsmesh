# OPSMESH

**Evidence-aware, human-supervised B2B lead-to-opportunity operations agent.**

OPSMESH is an AI agent that automates B2B lead outreach — research, qualification,
email drafting, and CRM sync — with human approval at every gate. No email goes
out without your sign-off.

## How it works

```
Lead intake → Research → Qualify → Draft → Critic → Gate 2 (approve) → Send → CRM sync → SENT
                Exa          Groq      Groq     Groq       Human          Resend    HubSpot
```

1. A lead enters via manual form, Tally webhook, or generic HMAC webhook
2. The **Research specialist** searches the web (Exa) and synthesizes findings (Groq GPT-OSS 120B)
3. The **Qualify specialist** scores the lead and decides QUALIFIED / NEEDS_REVIEW / DISQUALIFIED
4. The **Draft specialist** writes a personalized outreach email grounded in the research
5. The **Critic specialist** reviews the draft for quality (lenient by default)
6. The run pauses at **Gate 2** — you review the drafted email and approve/reject
7. On approval, the email is sent via **Resend** and the lead is synced to **HubSpot CRM**

## Tech stack

| Layer | Technology |
|---|---|
| Backend | FastAPI, Python 3.12+ |
| LLM | Groq GPT-OSS 120B (OpenAI-compatible API) |
| Web search | Exa |
| Email | Resend |
| CRM | HubSpot |
| Database | SQLite (dev) / Postgres (production) |
| Auth | Google OAuth + session JWTs |
| Frontend | Vanilla HTML/CSS/JS (no build step) |
| Deployment | Render (web service + background worker) |

## Architecture

```
┌─────────────┐     ┌──────────────────┐     ┌─────────────┐
│  Frontend   │     │  OPSMESH API     │     │   Worker    │
│  (browser)  │────▶│  (FastAPI)       │◀──▶│  (asyncio)  │
└─────────────┘     │                  │     └──────┬──────┘
                    │  ┌────────────┐  │            │
                    │  │ StateStore │  │            │
                    │  │ JobQueue   │  │            │
                    │  │ LeadStore  │  │            │
                    │  │ ApprovalSt │  │            │
                    │  └────────────┘  │            │
                    └────────┬─────────┘            │
                             │                      │
                    ┌────────▼──────────────────────▼────────┐
                    │           Database (SQLite/Postgres)    │
                    └────────────────────────────────────────┘
                             │                      │
                    ┌────────▼───┐          ┌───────▼───────┐
                    │   Exa API  │          │   Groq API    │
                    │  (search)  │          │  (GPT-OSS)    │
                    └────────────┘          └───────────────┘
                             │                      │
                    ┌────────▼───┐          ┌───────▼───────┐
                    │ Resend API │          │  HubSpot API  │
                    │  (email)   │          │    (CRM)      │
                    └────────────┘          └───────────────┘
```

## Project structure

```
opsmesh/
├── opsmesh/
│   ├── api/              # FastAPI app + routes
│   │   ├── app.py        # App factory (CORS, lifespan, static mount)
│   │   ├── auth.py       # Google OAuth + session JWT verification
│   │   ├── auth_google.py # POST /api/v1/auth/google
│   │   └── routes/       # leads, runs, approvals, webhook, tally, etc.
│   ├── adapters/         # Provider adapters (Groq, Exa, Resend, HubSpot)
│   │   ├── db/           # SQLite + Postgres stores
│   │   ├── llm/          # Groq provider (GPT-OSS 120B)
│   │   ├── research/     # Exa provider
│   │   ├── email/        # Resend provider
│   │   └── crm/          # HubSpot provider
│   ├── specialists/      # Research, Qualify, Draft, Critic, Sending, CRM
│   ├── supervisor.py     # The orchestrator (16-state machine)
│   ├── state_machine.py  # State transitions + validation
│   ├── models.py         # Pydantic models (RunRecord, Lead, etc.)
│   ├── worker.py         # Background worker loop
│   ├── approval_gate.py  # Gate 1 + Gate 2 approval system
│   └── static/           # Frontend (HTML/CSS/JS, no build step)
├── tests/                # 739 tests (unit + integration)
├── alembic/              # Database migrations
├── render.yaml           # Render deployment config
├── Procfile              # Alternative deployment
├── requirements.txt      # Pinned dependencies
└── .env.example          # Configuration template
```

## Quick start

```bash
# Clone
git clone https://github.com/yourusername/opsmesh.git
cd opsmesh

# Create venv + install deps
python -m venv venv
source venv/bin/activate  # Linux/Mac
# .\venv\Scripts\Activate.ps1  # Windows
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env with your API keys (Groq, Exa, Resend, HubSpot, Google OAuth)

# Run the API
uvicorn opsmesh.api.app:create_app --factory --host 0.0.0.0 --port 8000 --reload

# Run the worker (separate terminal)
python -m opsmesh.worker

# Open http://localhost:8000/
```

## Configuration

All configuration is via environment variables (prefix `OPSMESH_`). See `.env.example` for the full list.

### Required for production

| Variable | Description |
|---|---|
| `OPSMESH_DATABASE_URL` | Postgres connection string |
| `OPSMESH_LLM_API_KEY` | Groq API key (`gsk_...`) |
| `OPSMESH_EXA_API_KEY` | Exa API key |
| `OPSMESH_EMAIL_API_KEY` | Resend API key (`re_...`) |
| `OPSMESH_CRM_API_KEY` | HubSpot private app token |
| `OPSMESH_WEBHOOK_SECRET` | HMAC secret for webhooks |
| `OPSMESH_SESSION_SECRET` | Secret for signing session JWTs |
| `OPSMESH_GOOGLE_OAUTH_CLIENT_ID` | Google OAuth client ID |

### Safety

| Variable | Default | Description |
|---|---|---|
| `OPSMESH_EMAIL_KILL_SWITCH` | `true` | Blocks all real email sends. Set to `false` after verifying your sender domain in Resend. |
| `OPSMESH_PRODUCTION_MODE` | `false` | When `true`, fails closed if database or webhook secret is not configured. |

## API endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/` | Landing page |
| GET | `/ui/login.html` | Login (Google OAuth or dev mode) |
| GET | `/ui/dashboard.html` | Dashboard overview |
| GET | `/ui/leads.html` | Leads list + create |
| GET | `/ui/runs.html` | Runs list + detail |
| GET | `/ui/approvals.html` | Approvals list + decision |
| GET | `/ui/integrations.html` | Provider health |
| GET | `/ui/settings.html` | Settings + account |
| GET | `/health` | Liveness probe |
| GET | `/api/v1/health` | Detailed health |
| GET | `/api/v1/leads` | List leads (paginated) |
| POST | `/api/v1/leads` | Create lead manually |
| GET | `/api/v1/runs` | List runs (filterable) |
| GET | `/api/v1/runs/{id}` | Run detail |
| GET | `/api/v1/runs/{id}/timeline` | Run timeline |
| GET | `/api/v1/approvals` | List pending approvals |
| GET | `/api/v1/approvals/{id}` | Approval detail |
| POST | `/api/v1/approvals/{id}/decision` | Submit decision |
| GET | `/api/v1/integrations` | Provider status |
| GET | `/api/v1/dashboard` | Dashboard data |
| GET | `/api/v1/metrics` | 8 ops metrics |
| POST | `/api/v1/events/new_lead` | HMAC webhook (generic) |
| POST | `/api/v1/events/tally` | Tally webhook |
| POST | `/api/v1/auth/google` | Google OAuth verify |
| GET | `/api/v1/auth/google/client-id` | Google client ID |

## State machine

OPSMESH uses a 16-state state machine with strict transition validation. No illegal
transition is ever persisted.

```
NEW → RESEARCHING → QUALIFYING → DRAFTING → CRITIC → AWAITING_APPROVAL (Gate 2)
                                                              ↓ (approve)
                                                          SENDING → CRM_UPDATING → SENT
```

Gate 1 (AWAITING_EVIDENCE_REVIEW) pauses the run when research is insufficient.
Gate 2 (AWAITING_APPROVAL) pauses before any email is sent. Both require human
decisions to resume.

## Testing

```bash
python -m pytest tests/ -q --ignore=tests/integration
# 739 passed

# Integration tests (require real API keys)
OPSMESH_LLM_API_KEY=gsk_... python -m pytest tests/integration/ -v
```

## Deployment (Render)

1. Push to GitHub
2. Create a new Web Service on Render from the GitHub repo
3. Create a Postgres database on Render
4. Set environment variables (see `.env.example`)
5. Create a Background Worker using `python -m opsmesh.worker` as the start command
6. Verify your sender domain in Resend
7. Set up Google OAuth credentials with the Render URL

See `render.yaml` for the full blueprint.

## Security

- Google OAuth for authentication (no anonymous access in production)
- Workspace isolation (users can only see their own leads/runs)
- HMAC signature verification on all webhooks
- Payload-hash binding on approvals (prevents modification after approval)
- Email kill switch (blocks all sends until explicitly disabled)
- Sanitized error messages (no secrets in logs or API responses)

## License

MIT
