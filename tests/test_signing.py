"""HMAC signing — the half a receiver has to be able to reproduce."""
import time

import pytest
from django.test import override_settings

from stapel_webhooks.signing import HmacSha256Signer, generate_secret, sign, verify

BODY = b'{"data":{"id":"1"},"type":"listing.published"}'
SECRET = "whsec_testtesttest"


class TestHeaderShape:
    def test_header_carries_timestamp_and_v1(self):
        header = HmacSha256Signer().sign(SECRET, BODY, 1755993600)
        assert header.startswith("t=1755993600,v1=")
        assert len(header.split("v1=")[1]) == 64  # sha256 hex

    def test_signature_is_a_known_vector(self):
        """Pinned so a refactor cannot silently change what receivers verify.

        The signed string is ``f"{t}.{body}"`` — the value below was computed
        with the stdlib, not with this module, so it is an independent check
        of the construction rather than a snapshot of it.
        """
        import hashlib
        import hmac as _hmac

        expected = _hmac.new(
            SECRET.encode(), b"1755993600." + BODY, hashlib.sha256
        ).hexdigest()
        assert HmacSha256Signer().digest(SECRET, BODY, 1755993600) == expected


class TestVerification:
    def test_roundtrip(self):
        signer = HmacSha256Signer()
        header = signer.sign(SECRET, BODY)
        assert signer.verify(SECRET, BODY, header) is True

    def test_tampered_body_fails(self):
        signer = HmacSha256Signer()
        header = signer.sign(SECRET, BODY)
        assert signer.verify(SECRET, BODY + b" ", header) is False

    def test_wrong_secret_fails(self):
        signer = HmacSha256Signer()
        assert signer.verify("other", BODY, signer.sign(SECRET, BODY)) is False

    def test_moved_timestamp_invalidates_the_digest(self):
        """The timestamp is INSIDE the signed string: an attacker who
        rewrites t to dodge the tolerance window breaks v1."""
        signer = HmacSha256Signer()
        header = signer.sign(SECRET, BODY, 1000)
        forged = header.replace("t=1000", "t=2000")
        assert signer.verify(SECRET, BODY, forged, tolerance=0) is False

    def test_replay_outside_tolerance_is_refused(self):
        signer = HmacSha256Signer()
        old = int(time.time()) - 3600
        header = signer.sign(SECRET, BODY, old)
        assert signer.verify(SECRET, BODY, header, tolerance=300) is False
        assert signer.verify(SECRET, BODY, header, tolerance=0) is True

    def test_rotation_accepts_either_secret(self):
        signer = HmacSha256Signer()
        header = signer.sign("old-secret", BODY)
        assert signer.verify(["new-secret", "old-secret"], BODY, header) is True
        assert signer.verify(["new-secret"], BODY, header) is False

    @pytest.mark.parametrize("header", ["", "garbage", "t=abc,v1=ff", "v1=ff", "t=1"])
    def test_malformed_headers_are_refused(self, header):
        assert HmacSha256Signer().verify(SECRET, BODY, header, tolerance=0) is False


class TestSeam:
    def test_module_level_pair_uses_the_configured_signer(self):
        assert verify(SECRET, BODY, sign(SECRET, BODY)) is True

    @override_settings(STAPEL_WEBHOOKS={"SECRET_BYTES": 8})
    def test_secret_length_is_a_setting(self):
        assert len(generate_secret()) >= 8
        assert generate_secret() != generate_secret()


class TestSchemeIsConfigurable:
    """The element name is the receiver's vocabulary as much as ours: a host
    migrating from another provider keeps what its receivers already parse."""

    @override_settings(STAPEL_WEBHOOKS={"SIGNATURE_SCHEME": "sha256"})
    def test_signing_and_verifying_use_the_configured_scheme(self):
        signer = HmacSha256Signer()
        header = signer.sign(SECRET, BODY)
        assert ",sha256=" in header
        assert signer.verify(SECRET, BODY, header) is True

    @override_settings(STAPEL_WEBHOOKS={"SIGNATURE_SCHEME": "sha256"})
    def test_a_header_under_another_scheme_does_not_verify(self):
        header = "t=1,v1=" + HmacSha256Signer().digest(SECRET, BODY, 1)
        assert HmacSha256Signer().verify(SECRET, BODY, header, tolerance=0) is False
