# OPSMESH

**Evidence-aware, human-supervised B2B lead-to-opportunity operations agent.**

OPSMESH researches each inbound lead, qualifies it, drafts a personalized outreach email, then **pauses for a human approval** before anything sends. Every action is checkpointed, every email is gated by a signed approval, every claim is checked against evidence, and every tool call routes through a policy-enforcing gateway.

- **Two human gates**: Gate 1 pauses when research is insufficient; Gate 2 pauses before any email is sent.
- **Hardened tool gateway**: approvals are payload-hash-bound, single-use, workspace-scoped, and rate-limited — a forged or replayed send is rejected before the provider is ever called.
- **Input + output guardrails**: PII is redacted before the LLM sees it, prompt-injection patterns are blocked, drafts are length-capped and recipient-validated.
- **32-scenario eval suite**: deterministic, no API keys, runs in CI.

---

## How it works

```
Lead arrives (manual / webhook / embed widget)
   ↓
RESEARCH  →  QUALIFY  →  DRAFT  →  CRITIC  →  AWAITING_APPROVAL (Gate 2)
   (Exa)      (Groq)     (Groq)    (Groq)         ↓ human approves
                                          SENDING → CRM_UPDATING → SENT
                                          (Resend)   (HubSpot)
```

- If research is insufficient → **Gate 1** (AWAITING_EVIDENCE_REVIEW), human decides whether to proceed, request more info, or stop.
- If the critic rejects → draft retries (max 2) with feedback, then fails closed.
- If the human rejects at Gate 2 → run ends, no email sent.
- Every state transition is validated against a 16-state machine; no illegal transition is ever persisted.

## Tech stack

| Layer | Choice |
|---|---|
| Web framework | FastAPI 0.115 + Uvicorn |
| LLM | Groq (GPT-OSS 120B) |
| Research | Exa web search |
| Email | Resend |
| CRM | HubSpot |
| Database | SQLite (dev) / Postgres (prod) via aiosqlite + SQLAlchemy |
| Frontend | Static HTML/CSS/JS (no build step) |
| Auth | Single-user demo mode / Google OAuth + session JWT |
| Eval | 32 deterministic scenarios, mock providers |

---

## Project structure

```
opsmesh/
├── opsmesh/
│   ├── api/              # FastAPI app + routes
│   │   ├── app.py        # App factory (CORS, lifespan, static mount, public-CORS middleware)
│   │   ├── auth.py       # Session JWT verification + demo/dev auth
│   │   └── routes/       # leads, runs, approvals, webhook, tally, dashboard, integrations
│   ├── adapters/         # Provider adapters
│   │   ├── db/           # SQLite + Postgres stores (lead_store, state_store, approval_store, …)
│   │   ├── llm/          # Groq + mock providers
│   │   ├── research/     # Exa + mock providers
│   │   ├── email/        # Resend provider
│   │   └── crm/          # HubSpot provider
│   ├── specialists/      # Research, Qualify, Draft, Critic, Sending, CRM (LLM-driven)
│   ├── guardrails/       # Input + output guardrails (PII, injection, length, recipient, schema)
│   │   ├── base.py       # GuardrailChain + 3-valued result (passed/failed/error)
│   │   ├── input.py      # PIIRedactor + PromptInjectionHeuristic
│   │   ├── output.py     # OutputSchemaValidator, GroundingChecker, LengthCap, RecipientValidator
│   │   └── wiring.py     # Connects the chains to the runtime specialists
│   ├── eval/             # Eval runner + 32 scenarios + mock specialists
│   │   ├── runner.py     # EvalRunner (argparse CLI, auto-approves Gate 2 to exercise sends)
│   │   ├── scenarios.jsonl  # 32 scenarios: happy path, budget, security, failure-injection
│   │   └── mocks.py      # EvalResearchSpecialist … EvalCRMUpdatingSpecialist
│   ├── supervisor.py     # The orchestrator (dynamic step loop, checkpoint/resume)
│   ├── state_machine.py  # 16-state machine + transition validation
│   ├── approval_gate.py  # Gate 1 + Gate 2 (request/decide/resume, single-use consumption)
│   ├── approval_store.py # Atomic verify_and_consume (8 checks, CAS under lock)
│   ├── tool_gateway.py   # Policy enforcement: scope/approval/hash/allowlist/rate-limit/idempotency/audit
│   ├── worker.py         # Background worker loop (lease-based job queue)
│   ├── models.py         # Pydantic models (RunRecord, Lead, DraftResult, …)
│   ├── config.py         # Settings (env-prefixed, fail-closed in production)
│   └── static/           # Frontend (dashboard, leads, runs, approvals, integrations, embed.js)
├── tests/                # Unit + integration tests
├── alembic/              # Database migrations
├── scripts/              # Helper scripts (seed, db_reset, repro_gate2_bug, fix_eval_scenarios)
├── render.yaml           # Render deployment blueprint
├── Makefile              # demo, test, eval, ci, lint, db-* targets
├── requirements.txt      # Pinned dependencies
└── .env.example          # Configuration template
```

---

## Quick start

```bash
# Clone
git clone https://github.com/yourusername/opsmesh.git
cd opsmesh

# Create venv + install deps
python -m venv venv
source venv/bin/activate        # Linux/Mac
# .\venv\Scripts\Activate.ps1   # Windows
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env — set OPSMESH_LLM_API_KEY (Groq) + OPSMESH_EXA_API_KEY at minimum.
# Mock providers are used for any key left blank, so you can explore the UI
# before wiring real providers.

# Run the API + in-process worker (one command, demo mode)
uvicorn opsmesh.api.app:create_app --factory --host 0.0.0.0 --port 8000 --reload

# Or run the worker separately (production)
python -m opsmesh.worker

# Open http://localhost:8000/  →  a first-run hero explains the 3-step flow.
```

> **Demo mode** (`OPSMESH_ACCESS_MODE=single_user_demo`, the default) runs the worker in-process, skips auth, and uses SQLite at `opsmesh.db`. No API keys are required to explore — mock providers return deterministic data and the email kill switch is ON by default.

---

## Configuration

All configuration is via environment variables (prefix `OPSMESH_`). See `.env.example` for the full list.

### Providers

| Variable | Description |
|---|---|
| `OPSMESH_LLM_API_KEY` | Groq API key (`gsk_...`). Blank → mock LLM. |
| `OPSMESH_EXA_API_KEY` | Exa API key for web research. Blank → mock. |
| `OPSMESH_EMAIL_API_KEY` | Resend API key (`re_...`). Blank → mock. |
| `OPSMESH_CRM_API_KEY` | HubSpot private app token. Blank → mock. |
| `OPSMESH_DATABASE_URL` | Postgres connection string (prod). Blank → SQLite at `OPSMESH_DATABASE_PATH`. |

### Integrations (connect your website)

| Variable | Description |
|---|---|
| `OPSMESH_WEBHOOK_SECRET` | HMAC secret for the generic webhook (`/events/new_lead`). |
| `OPSMESH_TALLY_WEBHOOK_SECRET` | HMAC secret for the Tally webhook (`/events/tally`). |
| `OPSMESH_PUBLIC_TOKEN` | Publishable token for the embed widget (`/public/lead`). Safe to expose in browser JS — can only create leads. Blank → embed endpoint disabled. |
| `OPSMESH_CORS_ALLOWED_ORIGINS` | Comma-separated explicit origins for the dashboard API. (The public embed path allows `*` independently.) |

### Safety

| Variable | Default | Description |
|---|---|---|
| `OPSMESH_EMAIL_KILL_SWITCH` | `true` | Blocks all real email sends. Set `false` only after verifying your sender domain in Resend and setting `OPSMESH_EMAIL_TEST_RECIPIENT`. |
| `OPSMESH_PRODUCTION_MODE` | `false` | When `true`, fails closed if the database or webhook secret is not configured. |
| `OPSMESH_ACCESS_MODE` | `single_user_demo` | `single_user_demo` (no auth) or `multi_user` (Google OAuth + session JWT). |

---

## Connect your website

Three ways to feed leads into OPSMESH automatically. Open **Integrations → Connect your website** in the dashboard for copy-paste snippets.

### 1. Webhook (server-side, HMAC-signed) — for sites with a backend

```bash
BODY='{"company":"Acme Corp","contact_email":"founder@acme.com","submission_id":"uuid-1"}'
SECRET='your-opsmesh-webhook-secret'
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $NF}')
curl -X POST https://YOUR_HOST/api/v1/events/new_lead \
  -H "Content-Type: application/json" \
  -H "X-Signature: $SIG" \
  --data-raw "$BODY"
# → 202 {"status":"accepted","lead_id":"...","run_id":"..."}
```

Duplicate `submission_id`s are ignored automatically.

### 2. Embed widget (no backend) — for static sites

Set `OPSMESH_PUBLIC_TOKEN=pk_live_xxxxx` on the server (a publishable key, Stripe-style — safe to expose in browser JS; it can only create leads). Then add to any HTML page:

```html
<script src="https://YOUR_HOST/ui/embed.js" async></script>
<div id="opsmesh-form"
     data-token="pk_live_xxxxx"
     data-lead-type="individual"
     data-heading="Get in touch"
     data-color="#6366f1"></div>
```

The widget auto-derives the endpoint from where `embed.js` loads (no `data-endpoint` needed). It renders a styled form, validates the email server-side and client-side, traps bots with a hidden honeypot, and POSTs to `/api/v1/public/lead` (CORS-open, rate-limited 10/min, constant-time token compare). Optional attributes: `data-redirect`, `data-color`, `data-heading`, `data-subheading`, `data-lead-type` (`company` | `individual`). Check whether it's live: `GET /api/v1/public/status` → `{"enabled": true}`. The Integrations page has a **Generate token** button and a **Send test lead** button that prove the flow end-to-end.

### 3. Tally form

Tally does **not** sign webhooks, so put the secret in the URL path. Set `OPSMESH_TALLY_WEBHOOK_SECRET=tok_xxx` on the server, then in Tally → Integrations → Webhooks, paste `https://YOUR_HOST/api/v1/events/tally/tok_xxx`. Every submission becomes a researched lead (company/individual auto-detected from the fields). The HMAC variant `POST /api/v1/events/tally` (with a `tally-signature` header) is also available for providers that do sign payloads.

---

## Guardrails

OPSMESH runs defense-in-depth at three layers:

### Input guardrails (before the LLM sees lead fields)

Wired into `ResearchSpecialist` and `DraftSpecialist` via `guardrails/wiring.py`:

- **PIIRedactor** — masks emails (`u***@domain.com`), phones, credit cards in the prompt. The DB stores raw PII for the operator; only the LLM-bound payload is redacted.
- **PromptInjectionHeuristic** — blocks known injection patterns ("ignore previous instructions", "you are now", etc.) in company name / notes. On detection, the run routes to Gate 1 for human review (fail-safe, not auto-fail).

### Output guardrails (before a draft becomes an approval payload)

Wired into `DraftSpecialist`:

- **OutputSchemaValidator** — LLM output must match the `DraftResult` schema.
- **LengthCap(250)** — draft body hard-capped at 250 words.
- **RecipientValidator** — draft recipient must match the lead's contact email (prevents a compromised LLM from swapping the recipient post-approval).

### Tool gateway (at every side-effecting tool call)

`tool_gateway.py` enforces, on every `send_email` / `update_lead_status`:

1. Tool exists + policy lookup (unknown tool → rejected).
2. Auth scope check (`email.send`, `crm.write`, …).
3. Approval existence / expiration / consumption / APPROVED status / workspace match / run match / action match / **payload_hash match** (8 checks, atomic CAS under lock).
4. Arg allowlist (e.g. `update_lead_status.status` constrained to 5 values).
5. Per-tool, per-workspace rate limit.
6. Idempotency (dedup concurrent/replayed calls).
7. Suppressed-contact enforcement (human approval does NOT override).
8. Audit log (success + every rejection reason).

The approval ID alone never authorizes a send — the payload hash binds the approved content to the actual send.

---

## Eval

OPSMESH ships a deterministic eval suite — **32 scenarios**, no API keys required, runs in ~2 seconds.

```bash
make eval
# or
OPSMESH_LLM_PROVIDER=mock python -m opsmesh.eval.runner --scenarios=all --output=eval_results.jsonl
```

The runner spins up a fresh `InMemoryStateStore` + mock specialists per scenario, drives the real `Supervisor.run()`, auto-approves Gate 2 where needed to exercise the send step, and asserts on final state. Exit code is 0 (all pass) or 1 (any fail).

| Category | Scenarios | What they cover |
|---|---|---|
| Happy path | 001–006 | qualified → awaiting_approval → sent |
| Budget | 007–009 | token / cost / tool-call limits → failed |
| Checkpoint/resume | 010, 014 | crash recovery, resume after human approve |
| Human gate | 015 | reject at Gate 2 → rejected |
| Security | 016–023 | injection, suppression, dedup, cross-workspace, gate mismatch, payload tamper, fake approval, expired approval |
| Failure injection | 024–032 | LLM/Exa/Resend/HubSpot timeouts + failures, malformed JSON, empty content, word-limit, unknown verdict |

Run `make ci` to gate merges on test + eval together.

---

## API endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/` | Landing page |
| GET | `/ui/dashboard.html` | Dashboard (first-run hero + KPIs + pending approvals) |
| GET | `/ui/leads.html` | Leads list + create (company or individual) |
| GET | `/ui/runs.html` | Runs list + detail + timeline |
| GET | `/ui/approvals.html` | Approvals list (inline subject + recipient) + decision |
| GET | `/ui/integrations.html` | Provider health + connect-your-website snippets |
| GET | `/ui/embed.js` | Drop-in lead-capture widget for external sites |
| GET | `/health` | Liveness probe |
| GET | `/api/v1/health` | Detailed health |
| GET | `/api/v1/leads` | List leads (paginated) |
| POST | `/api/v1/leads` | Create lead manually |
| GET | `/api/v1/runs` | List runs (filterable) |
| GET | `/api/v1/runs/{id}` | Run detail |
| GET | `/api/v1/runs/{id}/timeline` | Run timeline |
| GET | `/api/v1/approvals` | List pending approvals (with subject + recipient) |
| GET | `/api/v1/approvals/{id}` | Approval detail |
| POST | `/api/v1/approvals/{id}/decision` | Submit decision (approve / reject / request_changes) |
| GET | `/api/v1/integrations` | Provider status |
| GET | `/api/v1/dashboard` | Dashboard data |
| POST | `/api/v1/events/new_lead` | HMAC webhook (generic, server-side) |
| POST | `/api/v1/events/tally` | Tally webhook (HMAC-signed variant) |
| POST | `/api/v1/events/tally/{token}` | Tally webhook (URL-token variant — Tally doesn't sign) |
| GET | `/api/v1/public/status` | Whether the embed endpoint is enabled (no token leak) |
| POST | `/api/v1/public/lead` | Public lead endpoint (embed widget, token-gated, CORS-open, honeypot) |

---

## State machine

OPSMESH uses a 16-state state machine with strict transition validation. No illegal transition is ever persisted.

```
NEW → RESEARCHING → QUALIFYING → DRAFTING → CRITIC → AWAITING_APPROVAL (Gate 2)
                                                              ↓ (approve)
                                                          SENDING → CRM_UPDATING → SENT
```

Gate 1 (AWAITING_EVIDENCE_REVIEW) pauses when research is insufficient. Gate 2 (AWAITING_APPROVAL) pauses before any email is sent. Both require human decisions to resume. The supervisor's dynamic step loop recomputes the next step each iteration, so a critic rejection correctly routes back to drafting (no stale-plan bug).

---

## Testing

```bash
# Unit + integration tests (mock providers)
python -m pytest tests/ -q
# 750+ passed

# Eval suite (32 scenarios)
make eval

# CI gate (tests + eval)
make ci

# Integration tests (require real API keys)
OPSMESH_LLM_API_KEY=gsk_... OPSMESH_EXA_API_KEY=... python -m pytest tests/integration/ -v
```

---

## Deployment (Render)

1. Push to GitHub.
2. Create a Web Service on Render from the repo.
3. Create a Postgres database on Render; set `OPSMESH_DATABASE_URL`.
4. Set environment variables (see `.env.example`). Set `OPSMESH_PRODUCTION_MODE=true`.
5. Create a Background Worker with `python -m opsmesh.worker` as the start command.
6. Verify your sender domain in Resend; set `OPSMESH_EMAIL_KILL_SWITCH=false` + `OPSMESH_EMAIL_TEST_RECIPIENT`.
7. Set up Google OAuth credentials with the Render URL (if using `multi_user` mode).

See `render.yaml` for the full blueprint.

---

## Security

- Single-user demo mode or Google OAuth (no anonymous access in production).
- Workspace isolation — users can only see their own leads/runs/approvals.
- HMAC signature verification on all webhooks; publishable-token gating on the public embed endpoint.
- Payload-hash binding on approvals (prevents modification after approval).
- PII redaction + prompt-injection detection before the LLM sees lead fields.
- Length cap + recipient validation on every draft.
- Email kill switch (blocks all sends until explicitly disabled).
- Sanitized error messages (no secrets in logs or API responses).
- Rate limiting on every public write endpoint.

## License

MIT
