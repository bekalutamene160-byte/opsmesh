# OPSMESH Dependency Map

**Audit date:** 2026-11-12
**Repo:** `/home/z/my-project/opsmesh/` (commit `4ec6cdf`)

This document maps every declared dependency, every provider adapter, and the version drift between `requirements.txt`, the installed sandbox environment, and what the code actually imports. It is the basis for the dependency-change recommendations in `STAGE_A_PLAN.md`.

---

## 1. Declared dependencies (`requirements.txt`)

```
# --- Web framework ---
fastapi==0.115.6
uvicorn[standard]==0.34.0
jinja2==3.1.5
python-multipart==0.0.20
itsdangerous==2.2.0

# --- Validation ---
pydantic==2.10.4
pydantic-settings==2.7.0
email-validator==2.2.0

# --- Database ---
asyncpg==0.32.0
aiosqlite==0.22.1
sqlalchemy==2.1.8          # ← INVALID PIN (no such release; see §3)
alembic==1.14.0

# --- LLM + AI ---
google-generativeai==0.8.3  # ← WRONG PROVIDER (code uses Groq, not Gemini; see §4)

# --- MCP ---
mcp==1.0.0

# --- Observability ---
structlog==24.4.0
opentelemetry-api==1.29.0
opentelemetry-sdk==1.29.0

# --- Auth + HTTP ---
PyJWT==2.15.1
cryptography>=44.0.0
httpx==0.28.1

# --- Testing ---
pytest==8.3.4
pytest-asyncio==0.25.0
pytest-cov==6.0.0
```

**Missing from `requirements.txt`:**
- `resend` — the codebase does **not** use the Resend Python SDK (it calls `https://api.resend.com/emails` directly via `httpx` at `adapters/email/resend.py:75`), so this is intentional. Document it.
- `ruff`, `mypy` — dev dependencies assumed by the Makefile `lint` target but not declared anywhere. See §6.
- No `[project.optional-dependencies]` block in `pyproject.toml`; no `requirements-dev.txt`.

---

## 2. Installed versions in sandbox vs. pin (drift table)

Sandbox Python: 3.12.16 (venv at `/home/z/.venv`).

| Package | `requirements.txt` pin | Installed in sandbox | Drift? | Notes |
|---|---|---|---|---|
| fastapi | `0.115.6` | `0.128.0` | ✗ major-ish | API stable; tests pass |
| uvicorn[standard] | `0.34.0` | (not checked) | ? | |
| pydantic | `2.10.4` | `2.12.5` | ✗ minor | v2 API stable |
| pydantic-settings | `2.7.0` | `2.13.1` | ✗ minor | |
| httpx | `0.28.1` | `0.28.1` | ✓ | |
| pytest | `8.3.4` | `9.0.2` | ✗ major | tests pass under 9.x |
| pytest-asyncio | `0.25.0` | `1.3.0` | ✗ major | `asyncio_mode=auto` still works |
| aiosqlite | `0.22.1` | `0.22.1` | ✓ | installed manually |
| sqlalchemy | `2.1.8` | `2.0.x` (installed `>=2.0,<2.1`) | ✗ **pin invalid** | `2.1.8` does not resolve in pip; relaxed to install |
| alembic | `1.14.0` | installed manually | ? | |
| asyncpg | `0.32.0` | installed manually | ? | |
| PyJWT | `2.15.1` | installed manually | ? | |
| google-generativeai | `0.8.3` | installed manually | — | **wrong provider** — see §4 |
| structlog, opentelemetry-*, jinja2, itsdangerous, python-multipart, email-validator, mcp, pytest-cov | various | installed manually | — | all installed without error |

**Conclusion:** the suite passes with the installed (drifted) versions, but the pins in `requirements.txt` are stale and one (`sqlalchemy==2.1.8`) is invalid. A clean `pip install -r requirements.txt` on Render would fail on the sqlalchemy pin.

---

## 3. The `sqlalchemy==2.1.8` invalid pin (P2)

`requirements.txt` line: `sqlalchemy==2.1.8`.

As of the audit date, SQLAlchemy's 2.x release line does not have a `2.1.8` tag that pip can resolve from PyPI (the 2.0.x line is current). `pip install sqlalchemy==2.1.8` returns:
```
ERROR: Could not find a version that satisfies the requirement sqlalchemy==2.1.8
```

**Impact:** `render.yaml` buildCommand is `pip install -r requirements.txt`. A clean Render build would fail at this line. The current deploy must be surviving because the Render cache already has a compatible sqlalchemy, or because the pin was edited after the last successful build.

**Fix (Stage B):** repin to `sqlalchemy>=2.0,<2.1` (or the latest 2.0.x). This is a one-line change with no behavioral impact — the code uses SQLAlchemy only for the async engine in `adapters/db/connection.py` and the codebase does not depend on any 2.1-only feature.

---

## 4. The `google-generativeai` wrong-provider dependency (P3)

`requirements.txt` declares `google-generativeai==0.8.3` (the Google Gemini SDK).

**The OPSMESH LLM adapter is Groq, not Gemini.** `adapters/llm/groq.py:62` `GroqProvider` calls `https://api.groq.com/openai/v1/chat/completions` via `httpx`. There is no `import google.generativeai` anywhere in `opsmesh/` source (only the dep is declared).

This dependency is **dead weight** from an earlier design iteration. It also collides with the auth-removal work: the mandate removes Google OAuth, and leaving a Google SDK in the dep tree is misleading.

**Fix (Stage B):** remove `google-generativeai` from `requirements.txt`. Verify no test imports it (none do — grep confirms). Add `groq` is **not** needed either (the adapter uses `httpx` directly, OpenAI-compatible endpoint).

---

## 5. Provider adapter dependency map (what each adapter actually needs)

| Adapter | File | External endpoint | HTTP client | SDK needed? |
|---|---|---|---|---|
| Groq LLM | `adapters/llm/groq.py:62` | `https://api.groq.com/openai/v1/chat/completions` | `httpx` | No (OpenAI-compatible REST) |
| Exa research | `adapters/research/exa.py:28` | `https://api.exa.ai/search` | `httpx` | No |
| Resend email | `adapters/email/resend.py:52` | `https://api.resend.com/emails`, `GET /emails/{id}` | `httpx` | No (Resend SDK not used) |
| HubSpot CRM | `adapters/crm/hubspot.py:42` | `https://api.hubapi.com/crm/v3/objects/contacts` | `httpx` | No |
| Mock providers | `adapters/{llm,research,email,crm,notifier}/mock.py` | — | — | — |

**All four real adapters use `httpx==0.28.1` directly.** No vendor SDK is required. The dep tree is lean; the only spurious entry is `google-generativeai`.

---

## 6. Dev dependencies gap (P3)

The Makefile (`/home/z/my-project/opsmesh/Makefile`) defines:
```
lint:
    ruff check .
    mypy --strict opsmesh
```

But neither `ruff` nor `mypy` is declared in `requirements.txt` or `pyproject.toml` `[project.optional-dependencies]`. A contributor running `make lint` in a clean checkout gets `command not found`.

`pyproject.toml` does configure both tools:
```toml
[tool.ruff]
line-length = 100
target-version = "py311"
[tool.ruff.lint]
select = ["E", "F", "I", "N", "W", "UP", "B", "ASYNC"]
ignore = ["E501"]
[tool.mypy]
python_version = "3.11"
strict = true
```

**Fix (Stage B):** add `requirements-dev.txt` with `ruff==0.8.x`, `mypy==1.13.x`, plus `pytest-cov` (already in requirements.txt but belongs in dev). Or use `[project.optional-dependencies] dev = [...]` and document `pip install -e ".[dev]"`.

---

## 7. Dependency changes proposed for Stage B

| Change | File | Rationale | Risk |
|---|---|---|---|
| Repin `sqlalchemy==2.1.8` → `sqlalchemy>=2.0,<2.1` | `requirements.txt` | Invalid pin blocks clean build | None (tests pass on 2.0.x) |
| Remove `google-generativeai==0.8.3` | `requirements.txt` | Wrong provider; dead weight; collides with OAuth removal | None (no imports) |
| Add `requirements-dev.txt` with `ruff`, `mypy` | new file | Make `make lint` work in clean checkout | None |
| Add `pytest-asyncio` marker for `asyncio_mode=auto` compatibility | already configured | — | — |
| Consider pinning `httpx` transitively | `requirements.txt` | All 4 adapters depend on it | Low |
| No new runtime deps for the Stage B scope | — | The mandate's enhancements (configurable ICP, suppression persistence, ToolGateway wiring, reconciler wiring, frontend) can all be built on the existing stack | — |

**No new runtime dependencies are required for Stage B.** Every enhancement in the plan (ToolGateway wiring, durable sent-email/history stores, reconciler wiring, OAuth removal, demo-mode guards, frontend redesign, eval expansion) is achievable with FastAPI + Pydantic + SQLAlchemy + httpx + vanilla JS already in the tree. This respects the mandate's rule: "Do not add dependencies without explaining their purpose."

---

## 8. Transitive dependency notes

- `fastapi` 0.128 pulls `starlette`, `pydantic`, `anyio`, `typing-extensions` — all stable.
- `uvicorn[standard]` pulls `httptools`, `websockets`, `uvloop`, `watchfiles` — all optional speed-ups; the `[standard]` extra is appropriate for production.
- `alembic` 1.14 pulls `sqlalchemy>=1.3.0`, `Mako`, `MarkupSafe` — compatible with the relaxed `sqlalchemy>=2.0,<2.1` pin.
- `cryptography>=44.0.0` is a moving pin; the `>=` is fine but a build-from-source fallback on some Linux distros requires `libffi-dev` and `libssl-dev` headers. Render's Python runtime ships pre-built wheels, so no action needed.

---

## 9. Summary

| Question | Answer |
|---|---|
| Does `pip install -r requirements.txt` succeed in a clean env? | **No** — `sqlalchemy==2.1.8` is an invalid pin. |
| Are there spurious dependencies? | **Yes** — `google-generativeai` (wrong provider, unused). |
| Are dev dependencies declared? | **No** — `ruff`/`mypy` missing despite Makefile using them. |
| Does the suite pass with relaxed pins? | **Yes** — 739 passed in 10.94s. |
| Are new runtime deps needed for Stage B? | **No.** |
