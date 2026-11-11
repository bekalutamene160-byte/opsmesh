"""
OPSMESH Authentication — Google OAuth + session JWT verification.

Architecture (Phase 6 — replaces the Supabase JWT approach):

  1. User clicks "Sign in with Google" on the frontend.
  2. Google Identity Services returns a Google ID token (RS256 JWT).
  3. The frontend POSTs the ID token to POST /api/v1/auth/google.
  4. The backend (api/auth_google.py) verifies the Google ID token
     using Google's public JWKS, extracts the user identity, and
     issues its OWN session JWT (HS256, signed with OPSMESH_SESSION_SECRET).
  5. The frontend stores the session JWT in localStorage and sends it
     as `Authorization: Bearer <token>` on every API request.
  6. THIS module (auth.get_current_user) verifies the session JWT and
     extracts the user's identity + workspace_id.

SECURITY:
  - Session JWTs are signed with OPSMESH_SESSION_SECRET (HS256).
  - The Google ID token is verified ONCE (at /api/v1/auth/google), then
    discarded. We never store it; we issue our own session JWT instead.
  - This means we control session expiration, custom claims, and revocation.
  - workspace_id is NEVER taken from the request body — it's derived
    from the authenticated user's identity (sub claim).
  - In production_mode=True, missing session_secret → startup failure.

DEV MODE:
  - When OPSMESH_GOOGLE_OAUTH_CLIENT_ID is empty AND production_mode is
    False, get_current_user returns a default dev user. This allows local
    development without Google OAuth configured.
  - In production_mode=True, dev mode fallback is DISABLED (fail closed).

USAGE:
  from opsmesh.api.auth import get_current_user, require_workspace

  @app.get("/api/v1/runs")
  async def list_runs(user: dict = Depends(get_current_user)):
      workspace_id = user["workspace_id"]
      ...
"""

from __future__ import annotations

import os
from typing import Optional

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from opsmesh.config import get_settings

# Security scheme for Bearer token extraction
security = HTTPBearer(auto_error=False)


def _resolve_session_secret() -> str:
    """Resolve the session secret from settings or env.

    Order of precedence:
      1. settings.session_secret (pydantic-settings, reads OPSMESH_SESSION_SECRET)
      2. env var OPSMESH_SESSION_SECRET (fallback)
    Returns "" if not set.
    """
    settings = get_settings()
    return (
        getattr(settings, "session_secret", "")
        or os.environ.get("OPSMESH_SESSION_SECRET", "")
    )


async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> dict:
    """Verify the session JWT and return the user's identity.

    Returns a dict with:
        - user_id: the user's ID (from JWT 'sub' claim)
        - email: the user's email
        - workspace_id: the workspace this user belongs to

    Raises:
        401 Unauthorized if the token is missing, expired, or invalid.

    DEV MODE: When google_oauth_client_id is not set AND production_mode
    is False, this function returns a default dev user. This allows local
    development without Google OAuth. In production_mode=True, this
    fallback is DISABLED.
    """
    settings = get_settings()

    # Resolve the session secret (HS256) used to sign OUR session JWTs
    session_secret = _resolve_session_secret()
    google_client_id = getattr(settings, "google_oauth_client_id", "")

    # Determine the auth mode
    auth_configured = bool(google_client_id) and bool(session_secret)

    # DEV MODE: auth not configured → return default dev user
    if not auth_configured:
        if settings.production_mode:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=(
                    "Production mode: Google OAuth must be configured. "
                    "Set OPSMESH_GOOGLE_OAUTH_CLIENT_ID and "
                    "OPSMESH_SESSION_SECRET."
                ),
            )
        return {
            "user_id": "dev_user",
            "email": "dev@opsmesh.dev",
            "workspace_id": "default",
        }

    # PRODUCTION: verify the session JWT (HS256, signed with session_secret)
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
        )

    token = credentials.credentials

    try:
        # Decode and verify our session JWT
        payload = jwt.decode(
            token,
            session_secret,
            algorithms=["HS256"],
            audience="opsmesh",  # our own audience
        )

        user_id = payload.get("sub", "")
        email = payload.get("email", "")

        if not user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token: missing user ID",
            )

        # Derive workspace_id from the user (v1: one workspace per user)
        # Future: look up workspace membership from DB
        workspace_id = payload.get("workspace_id") or f"ws_{user_id[:12]}"

        return {
            "user_id": user_id,
            "email": email,
            "workspace_id": workspace_id,
        }

    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired. Please sign in again.",
        )
    except jwt.InvalidTokenError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid session token: {str(e)[:100]}",
        )


def require_workspace(workspace_id: str, user: dict) -> str:
    """Verify the user has access to the requested workspace.

    Returns the workspace_id if authorized.
    Raises 403 Forbidden if the user doesn't belong to this workspace.
    """
    if user["workspace_id"] != workspace_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied: workspace '{workspace_id}' does not belong to user '{user['user_id']}'",
        )
    return workspace_id


__all__ = ["get_current_user", "require_workspace", "security"]
