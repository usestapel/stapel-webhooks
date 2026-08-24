"""Outbound HTTP for webhook delivery — the one place a stranger's URL is
dialled.

A webhook target is the most hostile URL a deployment ever holds: it is
supplied by whoever created the subscription, dialled by a background worker
with no user watching, and retried. So this transport carries the full
``stapel_core.net.safe_fetch`` guard set, POST-shaped:

* **https only**, unless the host has confessed otherwise
  (``ALLOW_INSECURE_TARGETS``);
* **exact-host allowlist**, when ``ALLOWED_TARGET_HOSTS`` is non-empty;
* **DNS -> IP validation** through core's ``ip_is_forbidden`` — the single
  fleet definition of "not a routable public address" (private, loopback,
  link-local including 169.254.169.254, CGNAT, and the IPv4-mapped / 6to4 /
  NAT64 re-encodings of all of them);
* **anti-rebinding**: resolve, validate, then connect to *that exact IP*
  while presenting the real hostname for SNI, certificate validation and the
  ``Host`` header. There is no second lookup between check and connect;
* **redirects are never followed.** For a GET, following a re-validated
  redirect is reasonable; for a signed POST it is not — the body would be
  replayed at an address the subscription's owner never named. A 3xx is a
  permanent failure with a legible reason;
* **capped response read** — a receiver's body is diagnostics, never a
  memory lever;
* **total deadline** across connect, send and read.

Why the guard is re-stated here rather than reused wholesale: core's
``fetch_bytes`` is GET-only today and takes no body or headers. The IP
policy — the part that must never drift — IS imported from core
(``ip_is_forbidden``); what this module adds is the POST shape around it. A
``post_bytes`` in ``stapel_core.net`` is the right home for that, and is
filed as the follow-up in MODULE.md §11.

This is the ``TRANSPORT`` seam (``STAPEL_WEBHOOKS["TRANSPORT"]``): a host
that must go through an egress proxy, or a test that must not touch a
socket, swaps the class and nothing else in the module changes.
"""
from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from stapel_core.net.safe_fetch import ip_is_forbidden


@dataclass
class TransportResponse:
    """What the receiver answered. ``body`` is already truncated."""

    status: int
    body: str = ""
    headers: dict = field(default_factory=dict)


class TransportError(Exception):
    """A delivery attempt that never got a status code.

    ``retryable`` is the whole point of the class: a DNS blip and a refused
    scheme are both failures, but one deserves the ladder and the other
    deserves the dead-letter on the first attempt.
    """

    def __init__(self, code: str, message: str = "", *, retryable: bool = True) -> None:
        super().__init__(message or code)
        self.code = code
        self.retryable = retryable


def _validated_ip(host: str, port: int) -> ipaddress._BaseAddress:
    """Resolve *host* and refuse if ANY answer is a non-public address.

    "Any", not "the first": a name answering with a mix of public and
    private records is hostile, not "pick the good one".
    """
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise TransportError("dns_resolution_failed", f"cannot resolve {host!r}: {exc}") from exc
    if not infos:
        raise TransportError("dns_resolution_failed", f"no addresses for {host!r}")
    first = None
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError as exc:  # pragma: no cover — getaddrinfo returns literals
            raise TransportError("dns_resolution_failed", str(exc), retryable=False) from exc
        if ip_is_forbidden(ip):
            raise TransportError(
                "blocked_ip",
                f"{host!r} resolves to non-public address {ip}",
                retryable=False,
            )
        if first is None:
            first = ip
    return first


class SafeHttpsTransport:
    """The default transport. One request, no redirects, pinned IP."""

    #: Statuses a receiver may answer with that mean "later, not never".
    RETRYABLE_STATUSES = frozenset({408, 425, 429})

    def post(
        self,
        url: str,
        body: bytes,
        headers: dict | None = None,
        *,
        timeout: float | None = None,
        total_deadline: float | None = None,
        max_response_bytes: int | None = None,
        allowed_hosts=None,
        allow_insecure: bool | None = None,
    ) -> TransportResponse:
        from .conf import webhooks_settings

        if timeout is None:
            timeout = float(webhooks_settings.TIMEOUT_SECONDS or 10)
        if total_deadline is None:
            total_deadline = float(webhooks_settings.TOTAL_DEADLINE_SECONDS or 20)
        if max_response_bytes is None:
            max_response_bytes = int(webhooks_settings.MAX_RESPONSE_BYTES or 4096)
        if allowed_hosts is None:
            allowed_hosts = webhooks_settings.ALLOWED_TARGET_HOSTS or ()
        if allow_insecure is None:
            allow_insecure = bool(webhooks_settings.ALLOW_INSECURE_TARGETS)

        started = time.monotonic()
        parsed = urlsplit(url)
        scheme = (parsed.scheme or "").lower()
        if scheme not in ("https", "http"):
            raise TransportError("scheme_unsupported", f"refusing scheme {scheme!r}", retryable=False)
        if scheme == "http" and not allow_insecure:
            raise TransportError(
                "scheme_not_https",
                "refusing plaintext http target (STAPEL_WEBHOOKS['ALLOW_INSECURE_TARGETS'] is off)",
                retryable=False,
            )
        host = parsed.hostname
        if not host:
            raise TransportError("no_host", "url has no host", retryable=False)
        allowed = {h.lower() for h in allowed_hosts} if allowed_hosts else None
        if allowed is not None and host.lower() not in allowed:
            raise TransportError(
                "host_not_allowed", f"host {host!r} is not allowlisted", retryable=False
            )
        port = parsed.port or (443 if scheme == "https" else 80)

        if allow_insecure:
            # Confessed: no name validation at all, so an in-cluster
            # receiver (or a test server on localhost) is reachable.
            target = host
        else:
            target = str(_validated_ip(host, port))

        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        remaining = total_deadline - (time.monotonic() - started)
        if remaining <= 0:
            raise TransportError("deadline_exceeded", "deadline exceeded before connect")
        conn = self._connect(scheme, host, target, port, timeout=min(timeout, remaining))
        try:
            conn.request("POST", path, body=body, headers={"Host": host, **(headers or {})})
            response = conn.getresponse()
            status = response.status
            if status in (301, 302, 303, 307, 308):
                # Deliberately not followed: see the module docstring.
                raise TransportError(
                    "redirect_not_followed",
                    f"receiver answered {status}; a signed POST is never replayed at a redirect target",
                    retryable=False,
                )
            payload = response.read(max_response_bytes) or b""
            return TransportResponse(
                status=status,
                body=payload.decode("utf-8", "replace"),
                headers={k.lower(): v for k, v in response.getheaders()},
            )
        except TransportError:
            raise
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            raise TransportError("connection_failed", f"{type(exc).__name__}: {exc}") from exc
        finally:
            conn.close()

    def _connect(self, scheme: str, host: str, target: str, port: int, *, timeout: float):
        """Open the connection, pinning TCP to *target* with SNI for *host*.

        Kept as its own method so a test — or an egress-proxy subclass — can
        substitute the network without re-implementing the URL policy above.
        """
        if scheme == "http":
            return http.client.HTTPConnection(target, port, timeout=timeout)
        raw = socket.create_connection((target, port), timeout=timeout)
        try:
            context = ssl.create_default_context()
            sock = context.wrap_socket(raw, server_hostname=host)
        except Exception:
            raw.close()
            raise
        conn = http.client.HTTPSConnection(host, port, timeout=timeout)
        conn.sock = sock
        return conn

    def classify(self, status: int) -> tuple[bool, bool]:
        """``(ok, retryable)`` for a receiver's status code.

        2xx succeeds. 5xx and the three "later" 4xx are retryable. Every
        other 4xx is the receiver saying the request is wrong, which no
        amount of repetition fixes — it goes to the dead-letter on the first
        attempt rather than after eight identical refusals.
        """
        if 200 <= status < 300:
            return True, False
        if status in self.RETRYABLE_STATUSES or status >= 500:
            return False, True
        return False, False


def get_transport():
    """The configured transport instance (``STAPEL_WEBHOOKS["TRANSPORT"]``)."""
    from .conf import webhooks_settings

    transport = webhooks_settings.TRANSPORT
    return transport() if isinstance(transport, type) else transport


__all__ = [
    "SafeHttpsTransport",
    "TransportError",
    "TransportResponse",
    "get_transport",
]
