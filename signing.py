"""HMAC signing of outbound webhook bodies — and the verifier the receiver
needs.

The header is the industry shape (Stripe's, and the one every receiver
library already knows how to read)::

    X-Stapel-Signature: t=1755993600,v1=6f1e...c3

``v1`` is ``HMAC-SHA256(secret, f"{t}.{body}")`` over the **exact bytes that
were sent**, hex-encoded. The timestamp is inside the signed string, which
is what makes a captured-and-replayed request detectable: a receiver refuses
a ``t`` outside its tolerance window, and an attacker cannot move ``t``
without invalidating ``v1``.

Two design points worth stating because getting either wrong is silent:

- **the signature covers bytes, not a re-serialized object.** A receiver
  that verifies against ``json.dumps(request.json())`` will fail on key
  order or spacing sooner or later. ``sign`` takes ``bytes``, and the
  transport sends the same object it signed.
- **verification is constant-time and version-tolerant.** ``verify`` walks
  every ``v1=`` element present (there may be several during a secret
  rotation) with ``hmac.compare_digest``, so neither the comparison nor the
  rotation leaks timing.

This module is the ``SIGNER`` seam's default implementation
(``STAPEL_WEBHOOKS["SIGNER"]``). A host that must speak another provider's
signature dialect swaps the class; nothing else in the module knows the
header's shape.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets as _secrets
import time

#: Prefix of the timestamp element inside the header value.
TIMESTAMP_ELEMENT = "t"


def generate_secret(nbytes: int | None = None) -> str:
    """A fresh subscription signing secret (urlsafe, ~1.33 chars/byte)."""
    if nbytes is None:
        from .conf import webhooks_settings

        nbytes = int(webhooks_settings.SECRET_BYTES or 32)
    return _secrets.token_urlsafe(nbytes)


def _parse(header: str, scheme: str = "v1") -> tuple[int | None, list[str], dict[str, list[str]]]:
    """``t=..,v1=..,v1=..`` -> (timestamp, signatures for *scheme*, all elements)."""
    elements: dict[str, list[str]] = {}
    for part in (header or "").split(","):
        name, _, value = part.strip().partition("=")
        if not name or not value:
            continue
        elements.setdefault(name.strip(), []).append(value.strip())
    raw_ts = (elements.get(TIMESTAMP_ELEMENT) or [None])[0]
    try:
        timestamp = int(raw_ts) if raw_ts is not None else None
    except ValueError:
        timestamp = None
    return timestamp, elements.get(scheme, []), elements


class HmacSha256Signer:
    """The default signer: SHA-256, hex, ``t=…,v1=…``."""

    digestmod = hashlib.sha256

    @property
    def scheme(self) -> str:
        """Signature element name — ``STAPEL_WEBHOOKS["SIGNATURE_SCHEME"]``.

        Configurable because it is the receiver's vocabulary as much as
        ours: a host migrating from another provider's webhooks can keep the
        element name its existing receivers already parse. Read at call time
        so it is one setting, not a value frozen at import.
        """
        from .conf import webhooks_settings

        return str(webhooks_settings.SIGNATURE_SCHEME or "v1")

    def signed_payload(self, body: bytes, timestamp: int) -> bytes:
        return f"{int(timestamp)}.".encode("utf-8") + body

    def digest(self, secret: str, body: bytes, timestamp: int) -> str:
        return hmac.new(
            secret.encode("utf-8"),
            self.signed_payload(body, timestamp),
            self.digestmod,
        ).hexdigest()

    def sign(self, secret: str, body: bytes, timestamp: int | None = None) -> str:
        """The full header value for *body* under *secret*."""
        if timestamp is None:
            timestamp = int(time.time())
        return f"{TIMESTAMP_ELEMENT}={int(timestamp)},{self.scheme}={self.digest(secret, body, timestamp)}"

    def verify(
        self,
        secret: str | tuple | list,
        body: bytes,
        header: str,
        *,
        tolerance: int | None = None,
        now: int | None = None,
    ) -> bool:
        """Whether *header* authenticates *body* under *secret*.

        *secret* may be a collection — that is a secret rotation, where old
        and new are both accepted for the length of the overlap. Returns
        False for every failure mode (missing element, unparseable
        timestamp, drift beyond tolerance, wrong digest): a verifier that
        distinguishes them in its return value hands an attacker an oracle.
        """
        timestamp, signatures, _ = _parse(header, self.scheme)
        if timestamp is None or not signatures:
            return False
        if tolerance is None:
            from .conf import webhooks_settings

            tolerance = int(webhooks_settings.SIGNATURE_TOLERANCE_SECONDS or 0)
        if tolerance:
            reference = int(now if now is not None else time.time())
            if abs(reference - timestamp) > tolerance:
                return False
        candidates = [secret] if isinstance(secret, str) else list(secret or ())
        ok = False
        for candidate in candidates:
            if not candidate:
                continue
            expected = self.digest(candidate, body, timestamp)
            for signature in signatures:
                # No early exit: every candidate is compared with
                # compare_digest so a rotation cannot be timed either.
                ok = hmac.compare_digest(expected, signature) or ok
        return ok


def get_signer():
    """The configured signer instance (``STAPEL_WEBHOOKS["SIGNER"]``)."""
    from .conf import webhooks_settings

    signer = webhooks_settings.SIGNER
    return signer() if isinstance(signer, type) else signer


def sign(secret: str, body: bytes, timestamp: int | None = None) -> str:
    """Header value for *body*, through the configured signer."""
    return get_signer().sign(secret, body, timestamp)


def verify(secret, body: bytes, header: str, **kwargs) -> bool:
    """Verify *header* over *body*, through the configured signer.

    Exported for receivers that live inside the fleet: a stapel service on
    the other end of a webhook verifies with the same code that signed,
    instead of a second implementation that drifts.
    """
    return get_signer().verify(secret, body, header, **kwargs)


__all__ = [
    "TIMESTAMP_ELEMENT",
    "HmacSha256Signer",
    "generate_secret",
    "get_signer",
    "sign",
    "verify",
]
