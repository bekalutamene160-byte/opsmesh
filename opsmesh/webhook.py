"""
OPSMESH HMAC Webhook Verification.

Architecture correction (OPSMESH_Corrections Issue #14, #15):
  The original architecture's webhook handler verified HMAC on the
  PARSED-then-re-serialized JSON body. This is a security vulnerability:
  re-serialization can change whitespace, key order, or encoding, causing
  the signature to fail (false rejection) — or worse, an attacker could
  craft a payload that serializes differently on the receiver vs. sender,
  bypassing signature verification.

  The fix: verify HMAC on the RAW request body (the exact bytes received
  over the wire). Never parse JSON before verifying.

  Additional defenses:
    - Constant-time comparison via hmac.compare_digest (prevents timing attacks).
    - 401 on missing or invalid signature.
    - The secret MUST be passed explicitly by the caller (the route handler
      reads it from Settings.webhook_secret). verify_hmac called without a
      secret raises WebhookSignatureError — it never silently reads an env
      var. (Stage B Phase 1: removed the unprefixed WEBHOOK_SECRET fallback
      that always returned "" and silently failed closed.)

Usage:
    from opsmesh.webhook import verify_hmac
    raw_body = await request.body()  # bytes
    signature = request.headers.get("X-Signature")  # hex string
    if not verify_hmac(raw_body, signature, secret=settings.webhook_secret):
        raise HTTPException(401, "Invalid signature")
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Optional


class WebhookSignatureError(Exception):
    """Raised when HMAC signature verification fails."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"Webhook signature verification failed: {reason}")


def compute_signature(secret: str, raw_body: bytes) -> str:
    """
    Compute the HMAC-SHA256 signature of raw_body using secret.

    Args:
        secret: the shared secret (from Settings.webhook_secret).
        raw_body: the EXACT bytes received over the wire. NOT re-serialized
            JSON. The sender computes the signature on the bytes they send;
            the receiver must verify on the bytes they receive.

    Returns:
        Hex string of the HMAC-SHA256 digest.
    """
    return hmac.new(
        secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()


def verify_hmac(
    raw_body: bytes,
    signature: Optional[str],
    secret: Optional[str] = None,
) -> bool:
    """
    Verify the HMAC signature of raw_body.

    CRITICAL: raw_body must be the EXACT bytes received — NOT re-serialized
    JSON. If you parse then re-serialize the JSON, whitespace/key-order
    differences will cause verification to fail. Worse, an attacker could
    craft a payload that serializes differently on receiver vs. sender,
    bypassing verification. Always verify on the raw bytes.

    Args:
        raw_body: the exact bytes received in the request body.
        signature: the hex signature from the X-Signature header.
            None if the header is missing.
        secret: the shared secret. REQUIRED — callers pass Settings.webhook_secret.
            If None or empty, raises WebhookSignatureError ("no secret configured")
            rather than silently returning False. (Stage B Phase 1: removed the
            unprefixed WEBHOOK_SECRET env fallback that masked misconfiguration.)

    Returns:
        True if the signature is valid. False if the signature is missing,
        malformed, or doesn't match.

    Security:
        Uses hmac.compare_digest (constant-time comparison) to prevent
        timing attacks. A naive `==` comparison leaks information about
        how many bytes match, allowing an attacker to brute-force the
        signature byte-by-byte.
    """
    if secret is None or not secret:
        raise WebhookSignatureError("no secret configured")
    if signature is None or not signature:
        return False  # Missing signature header

    expected = compute_signature(secret, raw_body)

    # CRITICAL: use compare_digest (constant-time) not ==
    # == leaks timing info about matching prefix length
    return hmac.compare_digest(expected, signature)


def verify_hmac_or_raise(
    raw_body: bytes,
    signature: Optional[str],
    secret: Optional[str] = None,
) -> None:
    """Like verify_hmac, but raises WebhookSignatureError on failure.

    Useful for FastAPI handlers that want to return 401 on verification
    failure."""
    if not verify_hmac(raw_body, signature, secret):
        if signature is None:
            raise WebhookSignatureError("missing X-Signature header")
        raise WebhookSignatureError("signature mismatch")


__all__ = [
    "compute_signature",
    "verify_hmac",
    "verify_hmac_or_raise",
    "WebhookSignatureError",
]
