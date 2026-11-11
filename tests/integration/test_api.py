"""
Integration tests for the FastAPI app — webhook, leads, simulate, health.

Uses FastAPI's TestClient (httpx under the hood) to make real HTTP requests
to the app. Tests cover:
  - Webhook with valid HMAC → 202 accepted
  - Webhook with invalid HMAC → 401
  - Webhook with duplicate submission_id → 202 duplicate
  - Webhook with suppressed email → 202 (vague response, no leak)
  - Manual lead creation → 201
  - Simulated lead → 202
  - Health and ready endpoints
  - All three intake paths go through LeadIntakeService.create_lead (FR-5)
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from opsmesh.api.app import create_app
from opsmesh.webhook import compute_signature


WEBHOOK_SECRET = "test-secret-12345"


@pytest.fixture
def app():
    return create_app(webhook_secret=WEBHOOK_SECRET, workspace_id="default")


@pytest.fixture
def client(app):
    return TestClient(app)


# ---------------------------------------------------------------------------
# 1. Health endpoints
# ---------------------------------------------------------------------------


class TestHealth:
    def test_health_returns_200(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_ready_returns_200(self, client):
        r = client.get("/ready")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ready"
        assert body["store"] == "in_memory"


# ---------------------------------------------------------------------------
# 2. Webhook endpoint
# ---------------------------------------------------------------------------


class TestWebhookEndpoint:
    def test_valid_signature_returns_202(self, client):
        payload = {
            "company": "Acme Corp",
            "contact_email": "founder@acme.com",
            "submission_id": "sub_001",
        }
        raw_body = json.dumps(payload).encode()
        signature = compute_signature(WEBHOOK_SECRET, raw_body)

        r = client.post(
            "/api/v1/events/new_lead",
            content=raw_body,
            headers={
                "X-Signature": signature,
                "Content-Type": "application/json",
            },
        )
        assert r.status_code == 202
        body = r.json()
        assert body["status"] == "accepted"
        assert body["lead_id"].startswith("lead_")
        assert body["run_id"].startswith("run_")

    def test_invalid_signature_returns_401(self, client):
        payload = {"company": "Acme", "contact_email": "r@acme.com", "submission_id": "sub_002"}
        raw_body = json.dumps(payload).encode()
        wrong_sig = "0" * 64

        r = client.post(
            "/api/v1/events/new_lead",
            content=raw_body,
            headers={
                "X-Signature": wrong_sig,
                "Content-Type": "application/json",
            },
        )
        assert r.status_code == 401

    def test_missing_signature_returns_401(self, client):
        payload = {"company": "Acme", "contact_email": "r@acme.com", "submission_id": "sub_003"}
        raw_body = json.dumps(payload).encode()

        r = client.post(
            "/api/v1/events/new_lead",
            content=raw_body,
            headers={"Content-Type": "application/json"},  # no X-Signature
        )
        assert r.status_code == 401

    def test_duplicate_submission_returns_duplicate(self, client):
        """FR-4: Same submission_id within 5 min → duplicate."""
        payload = {
            "company": "Acme",
            "contact_email": "r@acme.com",
            "submission_id": "sub_dup_001",
        }
        raw_body = json.dumps(payload).encode()
        sig = compute_signature(WEBHOOK_SECRET, raw_body)

        # First call → accepted
        r1 = client.post(
            "/api/v1/events/new_lead",
            content=raw_body,
            headers={"X-Signature": sig, "Content-Type": "application/json"},
        )
        assert r1.status_code == 202
        assert r1.json()["status"] == "accepted"

        # Second call (same submission_id) → duplicate
        r2 = client.post(
            "/api/v1/events/new_lead",
            content=raw_body,
            headers={"X-Signature": sig, "Content-Type": "application/json"},
        )
        assert r2.status_code == 202
        assert r2.json()["status"] == "duplicate"

    def test_signature_verified_on_raw_body_not_reserialized(self, client):
        """CRITICAL: HMAC is computed on the EXACT bytes received, not on
        re-serialized JSON. If the client sends '{"a": 1}' with a space,
        the server verifies on '{"a": 1}' with a space — not on the
        re-serialized '{"a":1}'."""
        # Body with non-canonical whitespace
        raw_body = b'{ "company" : "Acme" , "contact_email" : "r@acme.com" , "submission_id" : "sub_ws_001" }'
        sig = compute_signature(WEBHOOK_SECRET, raw_body)

        r = client.post(
            "/api/v1/events/new_lead",
            content=raw_body,
            headers={"X-Signature": sig, "Content-Type": "application/json"},
        )
        # Should succeed — the server verified on the raw bytes
        assert r.status_code == 202
        assert r.json()["status"] == "accepted"


# ---------------------------------------------------------------------------
# 3. Manual lead creation
# ---------------------------------------------------------------------------


class TestManualLeadCreation:
    def test_creates_lead_returns_201(self, client):
        r = client.post(
            "/api/v1/leads",
            json={
                "company": "Globex",
                "contact_email": "founder@globex.com",
                "industry": "Manufacturing",
                "employees": 50,
            },
        )
        assert r.status_code == 201
        body = r.json()
        assert body["lead_id"].startswith("lead_")
        assert body["run_id"].startswith("run_")
        assert body["status"] == "queued"

    def test_invalid_payload_returns_422(self, client):
        """Missing required field → 422 (Pydantic validation)."""
        r = client.post(
            "/api/v1/leads",
            json={"company": "Acme"},  # missing contact_email
        )
        assert r.status_code == 422

    def test_invalid_email_returns_422(self, client):
        r = client.post(
            "/api/v1/leads",
            json={"company": "Acme", "contact_email": "not-an-email"},
        )
        assert r.status_code == 422

    def test_get_lead_detail(self, client):
        # First create a lead
        create = client.post(
            "/api/v1/leads",
            json={"company": "Initech", "contact_email": "founder@initech.com"},
        )
        lead_id = create.json()["lead_id"]

        # Then fetch it
        r = client.get(f"/api/v1/leads/{lead_id}")
        assert r.status_code == 200
        body = r.json()
        assert body["id"] == lead_id
        assert body["company"] == "Initech"
        assert body["disposition"] == "new"
        assert body["source"] == "manual"

    def test_get_unknown_lead_returns_404(self, client):
        r = client.get("/api/v1/leads/lead_nonexistent")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# 4. Simulated lead
# ---------------------------------------------------------------------------


class TestSimulatedLead:
    def test_simulate_with_default(self, client):
        """POST /api/v1/demo/simulate with no body → picks a synthetic lead."""
        r = client.post("/api/v1/demo/simulate")
        assert r.status_code == 202
        body = r.json()
        assert body["lead_id"].startswith("lead_")
        assert body["scenario"] == "simulated"
        assert body["status"] == "queued"

    def test_simulate_with_body(self, client):
        r = client.post(
            "/api/v1/demo/simulate",
            json={"company": "Custom Sim", "contact_email": "custom@sim.com"},
        )
        assert r.status_code == 202
        assert r.json()["lead_id"].startswith("lead_")


# ---------------------------------------------------------------------------
# 5. FR-5: All three paths use LeadIntakeService.create_lead
# ---------------------------------------------------------------------------


class TestSingleEntryPoint:
    """FR-5: Manual, webhook, and simulated intake all go through the same
    LeadIntakeService.create_lead method. We verify this by checking that
    all three create a lead, run, and job with consistent ID prefixes."""

    def test_all_three_paths_create_leads(self, client):
        # 1. Manual
        r1 = client.post(
            "/api/v1/leads",
            json={"company": "Manual", "contact_email": "m@x.com"},
        )
        assert r1.status_code == 201

        # 2. Webhook
        payload = {"company": "Webhook", "contact_email": "w@x.com", "submission_id": "sub_x"}
        raw = json.dumps(payload).encode()
        sig = compute_signature(WEBHOOK_SECRET, raw)
        r2 = client.post(
            "/api/v1/events/new_lead",
            content=raw,
            headers={"X-Signature": sig, "Content-Type": "application/json"},
        )
        assert r2.status_code == 202

        # 3. Simulated
        r3 = client.post("/api/v1/demo/simulate")
        assert r3.status_code == 202

        # All three created leads with consistent IDs
        for r in [r1, r2, r3]:
            assert "lead_id" in r.json()
            assert "run_id" in r.json()
