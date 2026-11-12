"""
Tests for opsmesh.webhook (HMAC verification).

CRITICAL: These tests verify the security-critical correction — HMAC must
be computed on the RAW body, NOT re-serialized JSON. This prevents both:
  1. False rejections (whitespace/key-order differences).
  2. Bypass attacks (an attacker crafts a payload that serializes
     differently on receiver vs. sender).
"""

from __future__ import annotations

import json

import pytest

from opsmesh.webhook import (
    WebhookSignatureError,
    compute_signature,
    verify_hmac,
    verify_hmac_or_raise,
)


SECRET = "test-secret-do-not-use-in-prod"


# ---------------------------------------------------------------------------
# 1. Valid signatures
# ---------------------------------------------------------------------------


class TestValidSignatures:
    def test_valid_signature_passes(self):
        raw_body = b'{"company":"Acme","email":"r@acme.com"}'
        signature = compute_signature(SECRET, raw_body)
        assert verify_hmac(raw_body, signature, secret=SECRET) is True

    def test_signature_on_raw_bytes_not_reserialized(self):
        """CRITICAL: the signature must verify on the EXACT bytes received.

        If we re-serialize JSON, whitespace differences break verification.
        The sender sends '{"a": 1}' but Python's json.dumps might produce
        '{"a": 1}' or '{"a":1}' depending on settings. Verifying on raw
        bytes is the only safe approach."""
        # Two JSON strings with the same data but different formatting
        body_with_space = b'{"company": "Acme"}'
        body_no_space = b'{"company":"Acme"}'
        # The signatures are DIFFERENT
        sig_space = compute_signature(SECRET, body_with_space)
        sig_no_space = compute_signature(SECRET, body_no_space)
        assert sig_space != sig_no_space
        # Each signature only verifies against its OWN raw body
        assert verify_hmac(body_with_space, sig_space, secret=SECRET) is True
        assert verify_hmac(body_no_space, sig_no_space, secret=SECRET) is True
        # Cross-verification FAILS (this is the security property)
        assert verify_hmac(body_with_space, sig_no_space, secret=SECRET) is False


# ---------------------------------------------------------------------------
# 2. Invalid signatures
# ---------------------------------------------------------------------------


class TestInvalidSignatures:
    def test_missing_signature_returns_false(self):
        assert verify_hmac(b'{"x":1}', None, secret=SECRET) is False

    def test_empty_signature_returns_false(self):
        assert verify_hmac(b'{"x":1}', "", secret=SECRET) is False

    def test_wrong_signature_returns_false(self):
        raw_body = b'{"company":"Acme"}'
        wrong_sig = "0" * 64  # valid hex length but wrong value
        assert verify_hmac(raw_body, wrong_sig, secret=SECRET) is False

    def test_tampered_body_returns_false(self):
        """If the body is modified after signing, verification fails."""
        original = b'{"company":"Acme"}'
        tampered = b'{"company":"Evil"}'
        sig = compute_signature(SECRET, original)
        assert verify_hmac(tampered, sig, secret=SECRET) is False

    def test_wrong_secret_returns_false(self):
        """If the receiver uses a different secret, verification fails."""
        raw_body = b'{"company":"Acme"}'
        sig = compute_signature("sender-secret", raw_body)
        assert verify_hmac(raw_body, sig, secret="receiver-secret") is False

    def test_no_secret_configured_raises(self):
        """Stage B Phase 1 (P2-3): an empty/unset secret raises
        WebhookSignatureError instead of silently returning False.

        The caller MUST pass Settings.webhook_secret explicitly. The old
        unprefixed WEBHOOK_SECRET env fallback that masked misconfiguration
        was removed; verify_hmac no longer reads env vars on the caller's
        behalf. Fail-closed is the correct behavior — a misconfigured
        secret should be loud, not silently reject every webhook.
        """
        raw_body = b'{"x":1}'
        sig = compute_signature(SECRET, raw_body)
        from opsmesh.webhook import WebhookSignatureError
        with pytest.raises(WebhookSignatureError, match="no secret configured"):
            verify_hmac(raw_body, sig, secret="")
        with pytest.raises(WebhookSignatureError, match="no secret configured"):
            verify_hmac(raw_body, sig, secret=None)


# ---------------------------------------------------------------------------
# 3. Constant-time comparison (timing attack defense)
# ---------------------------------------------------------------------------


class TestTimingAttackDefense:
    def test_compare_digest_is_used(self):
        """verify_hmac uses hmac.compare_digest (constant-time), not ==.

        This is verified indirectly: a wrong signature returns False
        regardless of how many bytes match the expected value."""
        raw_body = b'{"x":1}'
        expected = compute_signature(SECRET, raw_body)
        # A signature that matches the first 30 chars but not the rest
        wrong = expected[:30] + "0" * 34
        assert verify_hmac(raw_body, wrong, secret=SECRET) is False


# ---------------------------------------------------------------------------
# 4. verify_hmac_or_raise
# ---------------------------------------------------------------------------


class TestVerifyOrRaise:
    def test_valid_signature_does_not_raise(self):
        raw_body = b'{"x":1}'
        sig = compute_signature(SECRET, raw_body)
        # Should not raise
        verify_hmac_or_raise(raw_body, sig, secret=SECRET)

    def test_missing_signature_raises(self):
        with pytest.raises(WebhookSignatureError) as exc_info:
            verify_hmac_or_raise(b'{"x":1}', None, secret=SECRET)
        assert "missing" in str(exc_info.value).lower()

    def test_invalid_signature_raises(self):
        with pytest.raises(WebhookSignatureError) as exc_info:
            verify_hmac_or_raise(b'{"x":1}', "wrong", secret=SECRET)
        assert "mismatch" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# 5. compute_signature
# ---------------------------------------------------------------------------


class TestComputeSignature:
    def test_returns_hex_string(self):
        sig = compute_signature(SECRET, b'{"x":1}')
        # SHA256 hex is 64 chars
        assert len(sig) == 64
        # All chars are hex
        int(sig, 16)  # raises if not hex

    def test_deterministic(self):
        """Same secret + body → same signature."""
        sig1 = compute_signature(SECRET, b'{"x":1}')
        sig2 = compute_signature(SECRET, b'{"x":1}')
        assert sig1 == sig2

    def test_different_body_different_signature(self):
        sig1 = compute_signature(SECRET, b'{"x":1}')
        sig2 = compute_signature(SECRET, b'{"x":2}')
        assert sig1 != sig2
