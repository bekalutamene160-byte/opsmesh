"""
OPSMESH webhook test client — sends a signed webhook request to localhost:8000.

Run this in a SECOND terminal (after `uvicorn` is running in the first):

    python test_webhook.py

This avoids all PowerShell quoting issues — Python handles the HMAC
computation and HTTP request natively.
"""

import hashlib
import hmac
import json
import sys

import httpx  # already installed (it's a FastAPI dependency)


WEBHOOK_SECRET = "test-secret"
WEBHOOK_URL = "http://localhost:8000/api/v1/events/new_lead"


def sign(secret: str, raw_body: bytes) -> str:
    """Compute the HMAC-SHA256 signature (hex string)."""
    return hmac.new(
        secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()


def send_webhook(payload: dict, submission_id: str = None):
    """Send one signed webhook request. Prints the response."""
    if submission_id:
        payload["submission_id"] = submission_id

    # CRITICAL: serialize JSON once, then sign the EXACT bytes.
    # The server verifies HMAC on raw bytes, not re-serialized JSON.
    raw_body = json.dumps(payload).encode("utf-8")
    signature = sign(WEBHOOK_SECRET, raw_body)

    print(f"\n{'='*60}")
    print(f"POST {WEBHOOK_URL}")
    print(f"Payload: {payload}")
    print(f"Signature: {signature[:32]}...")

    response = httpx.post(
        WEBHOOK_URL,
        content=raw_body,  # send the EXACT bytes we signed
        headers={
            "X-Signature": signature,
            "Content-Type": "application/json",
        },
        timeout=10.0,
    )

    print(f"Status: {response.status_code}")
    print(f"Response: {response.json()}")
    return response


def send_invalid_signature(payload: dict):
    """Send a request with a WRONG signature — should get 401."""
    raw_body = json.dumps(payload).encode("utf-8")
    wrong_sig = "0" * 64  # valid hex length, wrong value

    print(f"\n{'='*60}")
    print(f"POST {WEBHOOK_URL} (INVALID signature)")
    print(f"Payload: {payload}")

    response = httpx.post(
        WEBHOOK_URL,
        content=raw_body,
        headers={
            "X-Signature": wrong_sig,
            "Content-Type": "application/json",
        },
        timeout=10.0,
    )
    print(f"Status: {response.status_code}  (expected 401)")
    print(f"Response: {response.text}")


def send_no_signature(payload: dict):
    """Send a request with NO signature header — should get 401."""
    raw_body = json.dumps(payload).encode("utf-8")

    print(f"\n{'='*60}")
    print(f"POST {WEBHOOK_URL} (NO signature)")

    response = httpx.post(
        WEBHOOK_URL,
        content=raw_body,
        headers={"Content-Type": "application/json"},  # no X-Signature
        timeout=10.0,
    )
    print(f"Status: {response.status_code}  (expected 401)")


def send_duplicate(submission_id: str):
    """Send the same submission_id twice — second should be 'duplicate'."""
    print(f"\n{'='*60}")
    print(f"Duplicate test (submission_id={submission_id})")
    payload = {
        "company": "Dup Corp",
        "contact_email": "dup@example.com",
        "submission_id": submission_id,
    }
    r1 = send_webhook(payload)
    r2 = send_webhook(payload)
    print(f"\n--> First:  {r1.json()}")
    print(f"--> Second: {r2.json()}  (should be status=duplicate)")


def test_health():
    """Quick check that the server is up."""
    print(f"\n{'='*60}")
    print("GET http://localhost:8000/health")
    try:
        r = httpx.get("http://localhost:8000/health", timeout=5.0)
        print(f"Status: {r.status_code}")
        print(f"Response: {r.json()}")
        return True
    except httpx.ConnectError:
        print("ERROR: Cannot connect to localhost:8000.")
        print("Start the server first with:")
        print('  python -c "import uvicorn; from opsmesh.api.app import create_app; uvicorn.run(create_app(webhook_secret=\'test-secret\'), host=\'0.0.0.0\', port=8000)"')
        return False


def test_manual_lead():
    """Test the manual lead creation endpoint (no HMAC needed)."""
    print(f"\n{'='*60}")
    print("POST http://localhost:8000/api/v1/leads (manual)")
    payload = {
        "company": "Manual Corp",
        "contact_email": "manual@example.com",
        "industry": "Software",
        "employees": 25,
    }
    r = httpx.post(
        "http://localhost:8000/api/v1/leads",
        json=payload,
        timeout=10.0,
    )
    print(f"Status: {r.status_code}")
    print(f"Response: {r.json()}")


def test_simulate():
    """Test the simulated lead endpoint."""
    print(f"\n{'='*60}")
    print("POST http://localhost:8000/api/v1/demo/simulate")
    r = httpx.post("http://localhost:8000/api/v1/demo/simulate", timeout=10.0)
    print(f"Status: {r.status_code}")
    print(f"Response: {r.json()}")


if __name__ == "__main__":
    print("OPSMESH Webhook Test Client")
    print("=" * 60)

    # 0. Check server is up
    if not test_health():
        sys.exit(1)

    # 1. Valid webhook
    send_webhook({
        "company": "Acme Corp",
        "contact_email": "founder@acme.com",
    }, submission_id="sub_001")

    # 2. Invalid signature → 401
    send_invalid_signature({
        "company": "Evil",
        "contact_email": "evil@evil.com",
        "submission_id": "sub_evil",
    })

    # 3. Missing signature → 401
    send_no_signature({
        "company": "No Sig",
        "contact_email": "nosig@example.com",
        "submission_id": "sub_nosig",
    })

    # 4. Duplicate submission_id → duplicate
    send_duplicate("sub_dup_001")

    # 5. Manual lead creation (no HMAC)
    test_manual_lead()

    # 6. Simulated lead (no HMAC)
    test_simulate()

    print(f"\n{'='*60}")
    print("All tests complete!")
    print("\nTo see the lead you just created, run:")
    print('  python test_webhook.py --list-leads')
