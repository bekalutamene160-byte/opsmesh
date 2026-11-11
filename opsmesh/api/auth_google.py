"""
OPSMESH Google OAuth — verify Google ID token, issue a session JWT.

Architecture (Phase 6):
  This endpoint is the bridge between Google's identity system and our
  session system. It runs ONCE per login, not on every API request.

  Flow:
    1. Frontend uses Google Identity Services to get an ID token.
    2. Frontend POSTs the ID token to /api/v1/auth/google.
    3. This endpoint verifies the ID token using Google's public JWKS:
       - Signature (RS256) verified against Google's JWKS
       - Issuer must be https://accounts.google.com (or accounts.google.com)
       - Audience must be our google_oauth_client_id
       - Token must not be expired
    4. On success, extract user_id (sub) + email + email_verified.
    5. Issue a SESSION JWT (HS256) signed with OPSMESH_SESSION_SECRET:
       - sub: user_id
       - email: email
       - workspace_id: ws_<user_id[:12]>
       - aud: "opsmesh"
       - exp: 7 days from now
       - iat: now
    6. Return {token: <session_jwt>, user: {...}} to the frontend.

SECURITY:
  - The Google ID token is verified ONCE here, then discarded.
  - We never store Google's ID token — only our own session JWT.
  - The session JWT can be revoked by changing OPSMESH_SESSION_SECRET
    (invalidates all sessions at once — coarse but simple for v1).
  - Future enhancement: add a sessions table for per-token revocation.

DEV MODE:
  When google_oauth_client_id is empty, this endpoint returns 503
  (Service Unavailable) with a helpful message. The frontend should
  fall back to the "Enter dashboard (dev mode)" button.
"""

from __future__ import annotations

import os
from datetime import timedelta
from typing import Optional

import jwt
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from opsmesh.config import get_settings
from opsmesh.models import utcnow


router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class GoogleAuthRequest(BaseModel):
    """Frontend sends the Google ID token after the GIS popup closes."""
    credential: str = Field(
        ...,
        description="The Google ID token (JWT, RS256) returned by Google Identity Services.",
    )


class GoogleAuthResponse(BaseModel):
    """Backend responds with a session JWT + user info."""
    token: str = Field(..., description="Session JWT (HS256). Send as Bearer token.")
    user: dict


class GoogleClientIdResponse(BaseModel):
    """The public Google OAuth client_id (safe to expose — used by GIS)."""
    client_id: str
    configured: bool


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


# Session JWT lifetime: 7 days. Google's ID tokens are 1 hour, which would
# force users to re-sign-in every hour — too aggressive. 7 days is a
# reasonable default; can be made configurable later.
SESSION_LIFETIME_DAYS = 7

# Google's JWKS URL — fetched on demand (cached by PyJWKClient).
# These are Google's public keys (RSA). We use them to verify the ID token
# signature. We never have Google's private keys.
GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"

# Accepted Google issuers (the ID token's 'iss' claim must match one of these).
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/google/client-id", response_model=GoogleClientIdResponse)
async def get_google_client_id():
    """Return the public Google OAuth client_id (for the GIS button).

    The Google OAuth client_id is PUBLIC — it's safe to expose to the
    frontend. Google's security model relies on the redirect URI being
    registered in the Google Cloud Console, not on the client_id being
    secret.

    This endpoint is unauthenticated so the login page can render the
    Google Sign In button before the user has a session.
    """
    settings = get_settings()
    client_id = getattr(settings, "google_oauth_client_id", "") or os.environ.get(
        "OPSMESH_GOOGLE_OAUTH_CLIENT_ID", ""
    )
    return GoogleClientIdResponse(
        client_id=client_id,
        configured=bool(client_id),
    )


@router.post("/google", response_model=GoogleAuthResponse)
async def google_auth(req: GoogleAuthRequest):
    """Verify a Google ID token and issue a session JWT.

    The frontend obtains the credential via Google Identity Services
    (the `google.accounts.id` JS library), then POSTs it here.
    """
    settings = get_settings()

    google_client_id = getattr(settings, "google_oauth_client_id", "") or os.environ.get(
        "OPSMESH_GOOGLE_OAUTH_CLIENT_ID", ""
    )
    session_secret = getattr(settings, "session_secret", "") or os.environ.get(
        "OPSMESH_SESSION_SECRET", ""
    )

    # Refuse if Google OAuth isn't configured
    if not google_client_id or not session_secret:
        raise HTTPException(
            status_code=503,
            detail=(
                "Google OAuth is not configured. Set OPSMESH_GOOGLE_OAUTH_CLIENT_ID "
                "and OPSMESH_SESSION_SECRET, or use dev mode (no auth)."
            ),
        )

    # Verify the Google ID token
    try:
        # Use PyJWKClient to fetch + cache Google's public keys.
        # This makes a network call ONLY on the first verification (or when
        # Google rotates keys — typically every 24-48h). Cached thereafter.
        jwk_client = jwt.PyJWKClient(GOOGLE_JWKS_URL)
        signing_key = jwk_client.get_signing_key_from_jwt(req.credential)

        payload = jwt.decode(
            req.credential,
            signing_key.key,
            algorithms=["RS256"],
            audience=google_client_id,
            options={"verify_iss": True},
            issuer=GOOGLE_ISSUERS,
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Google ID token has expired. Please sign in again.")
    except jwt.InvalidTokenError as e:
        raise HTTPException(401, f"Invalid Google ID token: {str(e)[:200]}")
    except Exception as e:
        # Network error fetching JWKS, or other unexpected issue
        raise HTTPException(
            502,
            f"Failed to verify Google ID token (network?): {str(e)[:200]}",
        )

    # Extract the user identity from the verified Google ID token
    user_id = payload.get("sub", "")
    email = payload.get("email", "")
    email_verified = payload.get("email_verified", False)

    if not user_id or not email:
        raise HTTPException(
            400,
            "Google ID token is missing required claims (sub, email).",
        )

    if not email_verified:
        raise HTTPException(
            400,
            "Google account email is not verified. Please verify your email with Google.",
        )

    # Derive workspace_id (v1: one workspace per user)
    workspace_id = f"ws_{user_id[:12]}" if user_id else "default"

    # Issue our own session JWT (HS256)
    now = utcnow()
    expires_at = now + timedelta(days=SESSION_LIFETIME_DAYS)

    session_payload = {
        "sub": user_id,
        "email": email,
        "workspace_id": workspace_id,
        "aud": "opsmesh",
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "iss": "opsmesh",
    }

    session_token = jwt.encode(
        session_payload,
        session_secret,
        algorithm="HS256",
    )

    return GoogleAuthResponse(
        token=session_token,
        user={
            "user_id": user_id,
            "email": email,
            "workspace_id": workspace_id,
        },
    )


# ---------------------------------------------------------------------------
# Helper: issue a dev session token (for tests + dev mode)
# ---------------------------------------------------------------------------


def issue_dev_session_token(session_secret: str, user_id: str = "dev_user") -> str:
    """Issue a session JWT for dev mode (no Google OAuth).

    Used by tests to generate valid session tokens without going through
    Google. NOT exposed as an API endpoint (dev mode doesn't require a
    token at all — get_current_user returns a dev user).
    """
    now = utcnow()
    expires_at = now + timedelta(days=SESSION_LIFETIME_DAYS)
    payload = {
        "sub": user_id,
        "email": f"{user_id}@opsmesh.dev",
        "workspace_id": "default",
        "aud": "opsmesh",
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "iss": "opsmesh",
    }
    return jwt.encode(payload, session_secret, algorithm="HS256")


__all__ = [
    "router",
    "GoogleAuthRequest",
    "GoogleAuthResponse",
    "GoogleClientIdResponse",
    "issue_dev_session_token",
    "SESSION_LIFETIME_DAYS",
]
