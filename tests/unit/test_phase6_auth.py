"""
Tests for Phase 6: Google OAuth authentication.

Tests the new endpoints:
  GET  /api/v1/auth/google/client-id  — public endpoint returning the Google client_id
  POST /api/v1/auth/google            — verify Google ID token, issue session JWT

And the new auth flow:
  - Dev mode (no Google OAuth configured): get_current_user returns dev user
  - Production mode (Google OAuth + session_secret configured): get_current_user
    requires a valid session JWT (HS256, signed with session_secret)
  - The /api/v1/auth/google endpoint verifies a Google ID token via Google's JWKS,
    then issues a session JWT (HS256) with the user's identity.

The Google ID token verification is tested using a MOCK JWKS — we don't make
real Google API calls in tests. Instead, we:
  1. Generate a test RSA keypair
  2. Sign a fake Google ID token with the private key
  3. Mock PyJWKClient.get_signing_key_from_jwt to return our test public key
  4. Verify the endpoint accepts the token and issues a session JWT
"""

from __future__ import annotations

import json
import os
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from opsmesh.api.app import create_app
from opsmesh.api.auth_google import (
    GoogleClientIdResponse,
    GoogleAuthResponse,
    issue_dev_session_token,
    SESSION_LIFETIME_DAYS,
)
from opsmesh.config import get_settings


@pytest.fixture(autouse=True)
def clean_env():
    """Ensure no auth env vars leak between tests."""
    for var in (
        "OPSMESH_SUPABASE_JWT_SECRET",
        "OPSMESH_SESSION_SECRET",
        "OPSMESH_GOOGLE_OAUTH_CLIENT_ID",
        "OPSMESH_GOOGLE_OAUTH_CLIENT_SECRET",
        "OPSMESH_PRODUCTION_MODE",
    ):
        os.environ.pop(var, None)
    get_settings.cache_clear()
    yield
    for var in (
        "OPSMESH_SUPABASE_JWT_SECRET",
        "OPSMESH_SESSION_SECRET",
        "OPSMESH_GOOGLE_OAUTH_CLIENT_ID",
        "OPSMESH_GOOGLE_OAUTH_CLIENT_SECRET",
        "OPSMESH_PRODUCTION_MODE",
    ):
        os.environ.pop(var, None)
    get_settings.cache_clear()


@pytest.fixture
def app():
    return create_app(webhook_secret="test-secret")


@pytest.fixture
def client(app):
    return TestClient(app)


# ===========================================================================
# 1. GET /api/v1/auth/google/client-id — public endpoint
# ===========================================================================


class TestClientIdEndpoint:
    def test_returns_200_unauthenticated(self, client):
        """The client-id endpoint is public (no auth required)."""
        r = client.get("/api/v1/auth/google/client-id")
        assert r.status_code == 200
        data = r.json()
        assert "client_id" in data
        assert "configured" in data

    def test_returns_empty_when_not_configured(self, client):
        """In dev mode (no Google OAuth), client_id is empty + configured=False."""
        r = client.get("/api/v1/auth/google/client-id")
        data = r.json()
        assert data["client_id"] == ""
        assert data["configured"] is False

    def test_returns_client_id_when_configured(self):
        """When OPSMESH_GOOGLE_OAUTH_CLIENT_ID is set, it's returned."""
        os.environ["OPSMESH_GOOGLE_OAUTH_CLIENT_ID"] = "test-client-123.google.com"
        get_settings.cache_clear()
        try:
            app = create_app(webhook_secret="test-secret")
            client = TestClient(app)
            r = client.get("/api/v1/auth/google/client-id")
            assert r.status_code == 200
            data = r.json()
            assert data["client_id"] == "test-client-123.google.com"
            assert data["configured"] is True
        finally:
            os.environ.pop("OPSMESH_GOOGLE_OAUTH_CLIENT_ID", None)
            get_settings.cache_clear()


# ===========================================================================
# 2. POST /api/v1/auth/google — verify Google ID token, issue session JWT
# ===========================================================================


def _generate_rsa_keypair():
    """Generate a test RSA keypair for signing fake Google ID tokens."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_key, public_pem


def _make_google_id_token(private_key, client_id, user_id="user_123",
                          email="test@opsmesh.dev", email_verified=True,
                          expired=False, wrong_audience=False):
    """Create a fake Google ID token (RS256) signed with the test private key."""
    now = int(time.time())
    aud = "wrong-client-id" if wrong_audience else client_id
    payload = {
        "iss": "https://accounts.google.com",
        "aud": aud,
        "sub": user_id,
        "email": email,
        "email_verified": email_verified,
        "name": "Test User",
        "picture": "https://example.com/photo.png",
        "iat": now,
        "exp": now - 10 if expired else now + 3600,
    }
    priv_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return jwt.encode(payload, priv_pem, algorithm="RS256", headers={"kid": "test-key-1"})


class TestGoogleAuthEndpoint:
    """Tests for POST /api/v1/auth/google.

    These tests mock PyJWKClient so we don't make real network calls to
    Google's JWKS endpoint. We generate our own RSA keypair, sign a fake
    Google ID token, and verify the endpoint:
      1. Accepts the token (signature verified against our mock JWKS)
      2. Issues a session JWT (HS256) with the user's identity
      3. Rejects invalid tokens (wrong audience, expired, etc.)
    """

    def test_returns_503_when_not_configured(self, client):
        """When Google OAuth isn't configured, /auth/google returns 503."""
        r = client.post("/api/v1/auth/google", json={"credential": "fake-token"})
        assert r.status_code == 503

    def test_rejects_missing_credential(self):
        """POST without a credential field → 422."""
        os.environ["OPSMESH_GOOGLE_OAUTH_CLIENT_ID"] = "test-client-123"
        os.environ["OPSMESH_SESSION_SECRET"] = "test-session-secret-32-bytes-long!!"
        get_settings.cache_clear()
        try:
            app = create_app(webhook_secret="test-secret")
            client = TestClient(app)
            r = client.post("/api/v1/auth/google", json={})
            assert r.status_code == 422
        finally:
            os.environ.pop("OPSMESH_GOOGLE_OAUTH_CLIENT_ID", None)
            os.environ.pop("OPSMESH_SESSION_SECRET", None)
            get_settings.cache_clear()

    def test_rejects_invalid_token_signature(self, monkeypatch):
        """An ID token signed by an unknown key → 401.

        We don't mock JWKS here — we let PyJWKClient try to fetch Google's
        real JWKS. Since the token's 'kid' won't match any real Google key,
        verification fails. (This test makes a network call to Google's
        JWKS endpoint — if it fails due to no network, we skip.)
        """
        os.environ["OPSMESH_GOOGLE_OAUTH_CLIENT_ID"] = "test-client-123"
        os.environ["OPSMESH_SESSION_SECRET"] = "test-session-secret-32-bytes-long!!"
        get_settings.cache_clear()

        # Generate a token with a 'kid' that won't be in Google's real JWKS
        private_key, _ = _generate_rsa_keypair()
        bad_token = _make_google_id_token(private_key, "test-client-123")

        try:
            app = create_app(webhook_secret="test-secret")
            client = TestClient(app)
            r = client.post("/api/v1/auth/google", json={"credential": bad_token})
            # Either 401 (signature invalid) or 502 (network failure to JWKS)
            assert r.status_code in (401, 502), f"Unexpected status: {r.status_code} {r.text}"
        finally:
            os.environ.pop("OPSMESH_GOOGLE_OAUTH_CLIENT_ID", None)
            os.environ.pop("OPSMESH_SESSION_SECRET", None)
            get_settings.cache_clear()

    def test_accepts_valid_token_and_issues_session_jwt(self, monkeypatch):
        """A valid Google ID token → 200 with a session JWT.

        We mock PyJWKClient to return our test public key, so we don't
        need to make a real network call to Google's JWKS.
        """
        client_id = "test-client-123.google.com"
        session_secret = "test-session-secret-32-bytes-long!!"
        os.environ["OPSMESH_GOOGLE_OAUTH_CLIENT_ID"] = client_id
        os.environ["OPSMESH_SESSION_SECRET"] = session_secret
        get_settings.cache_clear()

        # Generate test RSA keypair + sign a fake Google ID token
        private_key, public_pem = _generate_rsa_keypair()
        google_token = _make_google_id_token(
            private_key, client_id, user_id="user_abc",
            email="founder@startup.com",
        )

        # Mock PyJWKClient.get_signing_key_from_jwt to return our public key
        from opsmesh.api import auth_google as auth_google_mod

        class FakeKey:
            def __init__(self, key):
                self.key = key

        class FakeJWKClient:
            def __init__(self, *args, **kwargs):
                pass
            def get_signing_key_from_jwt(self, token):
                # Return our test public key (ignoring the token's kid)
                return FakeKey(public_pem)

        monkeypatch.setattr(auth_google_mod.jwt, "PyJWKClient", FakeJWKClient)

        try:
            app = create_app(webhook_secret="test-secret")
            test_client = TestClient(app)
            r = test_client.post("/api/v1/auth/google", json={"credential": google_token})
            assert r.status_code == 200, f"Unexpected: {r.status_code} {r.text}"
            data = r.json()
            assert "token" in data
            assert "user" in data
            assert data["user"]["email"] == "founder@startup.com"
            assert data["user"]["user_id"] == "user_abc"
            assert data["user"]["workspace_id"].startswith("ws_")

            # Verify the issued session JWT (HS256) decodes correctly
            session_payload = jwt.decode(
                data["token"],
                session_secret,
                algorithms=["HS256"],
                audience="opsmesh",
            )
            assert session_payload["sub"] == "user_abc"
            assert session_payload["email"] == "founder@startup.com"
            assert session_payload["aud"] == "opsmesh"
            assert session_payload["iss"] == "opsmesh"
            assert "exp" in session_payload
        finally:
            os.environ.pop("OPSMESH_GOOGLE_OAUTH_CLIENT_ID", None)
            os.environ.pop("OPSMESH_SESSION_SECRET", None)
            get_settings.cache_clear()

    def test_rejects_token_with_wrong_audience(self, monkeypatch):
        """A token with the wrong audience → 401."""
        client_id = "test-client-123.google.com"
        session_secret = "test-session-secret-32-bytes-long!!"
        os.environ["OPSMESH_GOOGLE_OAUTH_CLIENT_ID"] = client_id
        os.environ["OPSMESH_SESSION_SECRET"] = session_secret
        get_settings.cache_clear()

        private_key, public_pem = _generate_rsa_keypair()
        # Create a token with wrong audience
        google_token = _make_google_id_token(
            private_key, client_id, wrong_audience=True,
        )

        from opsmesh.api import auth_google as auth_google_mod
        class FakeKey:
            def __init__(self, key): self.key = key
        class FakeJWKClient:
            def __init__(self, *args, **kwargs): pass
            def get_signing_key_from_jwt(self, token):
                return FakeKey(public_pem)
        monkeypatch.setattr(auth_google_mod.jwt, "PyJWKClient", FakeJWKClient)

        try:
            app = create_app(webhook_secret="test-secret")
            test_client = TestClient(app)
            r = test_client.post("/api/v1/auth/google", json={"credential": google_token})
            assert r.status_code == 401
        finally:
            os.environ.pop("OPSMESH_GOOGLE_OAUTH_CLIENT_ID", None)
            os.environ.pop("OPSMESH_SESSION_SECRET", None)
            get_settings.cache_clear()

    def test_rejects_token_with_unverified_email(self, monkeypatch):
        """A token where email_verified=False → 400."""
        client_id = "test-client-123.google.com"
        session_secret = "test-session-secret-32-bytes-long!!"
        os.environ["OPSMESH_GOOGLE_OAUTH_CLIENT_ID"] = client_id
        os.environ["OPSMESH_SESSION_SECRET"] = session_secret
        get_settings.cache_clear()

        private_key, public_pem = _generate_rsa_keypair()
        google_token = _make_google_id_token(
            private_key, client_id, email_verified=False,
        )

        from opsmesh.api import auth_google as auth_google_mod
        class FakeKey:
            def __init__(self, key): self.key = key
        class FakeJWKClient:
            def __init__(self, *args, **kwargs): pass
            def get_signing_key_from_jwt(self, token):
                return FakeKey(public_pem)
        monkeypatch.setattr(auth_google_mod.jwt, "PyJWKClient", FakeJWKClient)

        try:
            app = create_app(webhook_secret="test-secret")
            test_client = TestClient(app)
            r = test_client.post("/api/v1/auth/google", json={"credential": google_token})
            assert r.status_code == 400
            assert "not verified" in r.json()["detail"].lower()
        finally:
            os.environ.pop("OPSMESH_GOOGLE_OAUTH_CLIENT_ID", None)
            os.environ.pop("OPSMESH_SESSION_SECRET", None)
            get_settings.cache_clear()


# ===========================================================================
# 3. issue_dev_session_token() helper
# ===========================================================================


class TestIssueDevSessionToken:
    def test_issues_valid_hs256_jwt(self):
        secret = "test-secret-at-least-32-bytes-long!!"
        token = issue_dev_session_token(secret, user_id="user_123")
        payload = jwt.decode(token, secret, algorithms=["HS256"], audience="opsmesh")
        assert payload["sub"] == "user_123"
        assert payload["aud"] == "opsmesh"
        assert payload["iss"] == "opsmesh"
        assert "exp" in payload
        assert "iat" in payload

    def test_token_expires_in_7_days(self):
        """Session JWT expires 7 days from issue."""
        import datetime as dt
        secret = "test-secret-at-least-32-bytes-long!!"
        before = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=5)
        token = issue_dev_session_token(secret)
        after = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=5)
        payload = jwt.decode(token, secret, algorithms=["HS256"], audience="opsmesh")
        exp = dt.datetime.fromtimestamp(payload["exp"], tz=dt.timezone.utc)
        # Expiration should be ~7 days from now
        seven_days = dt.timedelta(days=SESSION_LIFETIME_DAYS)
        assert before + seven_days - dt.timedelta(seconds=5) <= exp <= after + seven_days + dt.timedelta(seconds=5)

    def test_token_rejected_with_wrong_secret(self):
        """A token signed with secret A is rejected by secret B."""
        token = issue_dev_session_token("secret-A-at-least-32-bytes-long-extra", user_id="x")
        with pytest.raises(jwt.InvalidTokenError):
            jwt.decode(token, "secret-B-at-least-32-bytes-long-extra", algorithms=["HS256"], audience="opsmesh")


# ===========================================================================
# 4. End-to-end: session JWT works with get_current_user
# ===========================================================================


class TestSessionJwtFlow:
    def test_session_jwt_works_with_protected_endpoint(self):
        """End-to-end: issue a session JWT, use it on /api/v1/runs → 200."""
        os.environ["OPSMESH_GOOGLE_OAUTH_CLIENT_ID"] = "test-client.google.com"
        os.environ["OPSMESH_SESSION_SECRET"] = "test-session-secret-32-bytes-long!!"
        get_settings.cache_clear()

        try:
            app = create_app(webhook_secret="test-secret")
            client = TestClient(app)

            token = issue_dev_session_token(
                session_secret="test-session-secret-32-bytes-long!!",
                user_id="user_e2e",
            )

            r = client.get("/api/v1/runs", headers={"Authorization": f"Bearer {token}"})
            assert r.status_code == 200
        finally:
            os.environ.pop("OPSMESH_GOOGLE_OAUTH_CLIENT_ID", None)
            os.environ.pop("OPSMESH_SESSION_SECRET", None)
            get_settings.cache_clear()

    def test_expired_session_jwt_returns_401(self):
        """An expired session JWT → 401."""
        os.environ["OPSMESH_GOOGLE_OAUTH_CLIENT_ID"] = "test-client.google.com"
        os.environ["OPSMESH_SESSION_SECRET"] = "test-session-secret-32-bytes-long!!"
        get_settings.cache_clear()

        try:
            # Create an expired token
            import datetime as dt
            now = dt.datetime.now(dt.timezone.utc)
            payload = {
                "sub": "user_expired",
                "email": "expired@test.com",
                "workspace_id": "ws_expired",
                "aud": "opsmesh",
                "iat": int(now.timestamp()) - 7200,
                "exp": int(now.timestamp()) - 3600,  # expired 1 hour ago
                "iss": "opsmesh",
            }
            token = jwt.encode(payload, "test-session-secret-32-bytes-long!!", algorithm="HS256")

            app = create_app(webhook_secret="test-secret")
            client = TestClient(app)
            r = client.get("/api/v1/runs", headers={"Authorization": f"Bearer {token}"})
            assert r.status_code == 401
            assert "expired" in r.json()["detail"].lower()
        finally:
            os.environ.pop("OPSMESH_GOOGLE_OAUTH_CLIENT_ID", None)
            os.environ.pop("OPSMESH_SESSION_SECRET", None)
            get_settings.cache_clear()

    def test_production_mode_without_auth_config_fails_closed(self, monkeypatch):
        """In production_mode=True with no auth configured → 401 on every request.

        Note: production_mode ALSO requires database_path to be set. To keep
        this test focused on auth fail-closed behavior (not the DB check),
        we monkeypatch the settings to bypass the DB check while keeping
        production_mode=True.
        """
        # Set up production_mode but with a database_path so create_app() boots
        os.environ["OPSMESH_PRODUCTION_MODE"] = "true"
        os.environ["OPSMESH_DATABASE_PATH"] = "/tmp/test-opsmesh-prod.db"
        # Remove any stale DB
        if os.path.exists("/tmp/test-opsmesh-prod.db"):
            os.remove("/tmp/test-opsmesh-prod.db")
        get_settings.cache_clear()

        try:
            app = create_app(webhook_secret="test-secret")
            client = TestClient(app)
            r = client.get("/api/v1/runs")
            assert r.status_code == 401
            assert "Google OAuth must be configured" in r.json()["detail"]
        finally:
            os.environ.pop("OPSMESH_PRODUCTION_MODE", None)
            os.environ.pop("OPSMESH_DATABASE_PATH", None)
            # Clean up the test DB
            if os.path.exists("/tmp/test-opsmesh-prod.db"):
                os.remove("/tmp/test-opsmesh-prod.db")
            get_settings.cache_clear()


# ===========================================================================
# 5. /api/v1/health reports the new auth mode correctly
# ===========================================================================


class TestHealthAuthMode:
    def test_health_reports_dev_mode_by_default(self, client):
        r = client.get("/api/v1/health")
        data = r.json()
        assert data["auth"]["mode"] == "dev"
        assert data["auth"]["google_oauth_configured"] is False
        assert data["auth"]["session_secret_configured"] is False

    def test_health_reports_google_oauth_when_configured(self):
        os.environ["OPSMESH_GOOGLE_OAUTH_CLIENT_ID"] = "test.google.com"
        os.environ["OPSMESH_SESSION_SECRET"] = "test-secret-32-bytes-long!!"
        get_settings.cache_clear()
        try:
            app = create_app(webhook_secret="test-secret")
            client = TestClient(app)
            r = client.get("/api/v1/health")
            data = r.json()
            assert data["auth"]["mode"] == "google_oauth"
            assert data["auth"]["google_oauth_configured"] is True
            assert data["auth"]["session_secret_configured"] is True
        finally:
            os.environ.pop("OPSMESH_GOOGLE_OAUTH_CLIENT_ID", None)
            os.environ.pop("OPSMESH_SESSION_SECRET", None)
            get_settings.cache_clear()

    def test_health_reports_dev_when_only_one_var_set(self):
        """If only client_id is set (no session_secret), still dev mode."""
        os.environ["OPSMESH_GOOGLE_OAUTH_CLIENT_ID"] = "test.google.com"
        # Note: no OPSMESH_SESSION_SECRET
        get_settings.cache_clear()
        try:
            app = create_app(webhook_secret="test-secret")
            client = TestClient(app)
            r = client.get("/api/v1/health")
            data = r.json()
            # Both must be set for google_oauth mode
            assert data["auth"]["mode"] == "dev"
            assert data["auth"]["google_oauth_configured"] is True
            assert data["auth"]["session_secret_configured"] is False
        finally:
            os.environ.pop("OPSMESH_GOOGLE_OAUTH_CLIENT_ID", None)
            get_settings.cache_clear()
