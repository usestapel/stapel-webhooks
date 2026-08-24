"""The four built-in deliveries — one mechanism, four last miles.

The design (docs/pending/forms-events-transports.md §3) draws the reaction
layer as one matcher feeding four deliveries. This module is the four; the
matcher is ``services.py`` and the choice between them is a row in
``registry.py``.

A handler receives a :class:`DeliveryContext` and returns a
:class:`DeliveryResult`. The contract has exactly one subtlety, and it is
the one that decides whether an outage costs a deployment its events:
``retryable`` says whether repeating the same attempt could plausibly
succeed. A receiver that is down is retryable; a receiver that answered 400
is not, and eight identical refusals teach nobody anything.

Handlers do not touch the ``Delivery`` row. The state machine belongs to
``services.attempt`` — a handler that flipped a status would be a second
place where "how many attempts are left" is decided.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


@dataclass
class DeliveryContext:
    """Everything a handler is allowed to know about one attempt."""

    delivery_id: str
    subscription_id: str
    event_type: str
    event_id: str
    payload: dict
    target: dict
    #: Signing secret, empty for unsigned delivery types.
    secret: str = ""
    attempt: int = 1
    created_at: datetime | None = None


@dataclass
class DeliveryResult:
    """The outcome of one attempt.

    ``retryable`` is only consulted when ``ok`` is false. A result that is
    neither ok nor retryable dead-letters immediately.
    """

    ok: bool
    retryable: bool = True
    status_code: int | None = None
    detail: str = ""
    headers: dict = field(default_factory=dict)


def envelope(context: DeliveryContext) -> dict:
    """The JSON body a webhook receiver sees.

    Stable and boring on purpose: ``id`` is the idempotency handle the
    receiver de-duplicates on, ``type`` is the event name it switches on,
    ``data`` is the emitting module's payload verbatim. Nothing about this
    module's internals leaks into it — no attempt counter, no subscription
    secret, no delivery-type name.
    """
    created = context.created_at or datetime.now(timezone.utc)
    return {
        "id": context.delivery_id,
        "type": context.event_type,
        "event_id": context.event_id,
        "created_at": created.astimezone(timezone.utc).isoformat(),
        "subscription_id": context.subscription_id,
        "data": context.payload,
    }


def encode_body(context: DeliveryContext) -> bytes:
    """Serialize the envelope to the EXACT bytes that will be signed.

    ``sort_keys`` and a fixed separator are not cosmetics: the signature
    covers bytes, and a receiver that re-serializes before verifying is
    already broken. Deterministic bytes at least make our half reproducible,
    so a support answer can be "sign this string and compare".
    """
    return json.dumps(
        envelope(context), sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")


# ── webhook ──────────────────────────────────────────────────────────


def deliver_webhook(context: DeliveryContext) -> DeliveryResult:
    """HTTP POST the envelope to the subscription's URL, HMAC-signed."""
    from .conf import webhooks_settings
    from .signing import sign
    from .transport import TransportError, get_transport

    body = encode_body(context)
    headers = {
        "Content-Type": "application/json",
        "User-Agent": str(webhooks_settings.USER_AGENT or "stapel-webhooks/1.0"),
        "Accept": "*/*",
        "Content-Length": str(len(body)),
        # The receiver's de-duplication handle: identical across our retries
        # by construction, because it IS the delivery row's identity.
        "X-Stapel-Delivery": context.delivery_id,
        "X-Stapel-Event": context.event_type,
        "X-Stapel-Event-Id": context.event_id,
        "X-Stapel-Attempt": str(context.attempt),
    }
    if context.secret:
        headers[str(webhooks_settings.SIGNATURE_HEADER or "X-Stapel-Signature")] = sign(
            context.secret, body
        )

    transport = get_transport()
    try:
        response = transport.post(context.target["url"], body, headers)
    except TransportError as exc:
        return DeliveryResult(
            ok=False, retryable=exc.retryable, detail=f"{exc.code}: {exc}"
        )
    ok, retryable = transport.classify(response.status)
    detail = "" if ok else (response.body or "")[:1000]
    return DeliveryResult(
        ok=ok,
        retryable=retryable,
        status_code=response.status,
        detail=detail,
        headers=response.headers,
    )


# ── notification ─────────────────────────────────────────────────────


def deliver_notification(context: DeliveryContext) -> DeliveryResult:
    """Hand the fact to stapel-notifications as a notification request.

    Reached through ``stapel_core.notifications.publish`` — a bus request by
    name, never an import of the notifications module. That is what lets a
    deployment run this delivery type with the notifications service in a
    different process, or not at all.

    The event payload rides as template ``variables``, so a host's template
    for the type can address any field of the emitting module's schema.
    """
    from stapel_core.notifications.publish import request_notification

    target = context.target
    try:
        queued = request_notification(
            target["notification_type"],
            user_id=target.get("user_id"),
            email=target.get("email"),
            phone=target.get("phone"),
            telegram_chat_id=target.get("telegram_chat_id"),
            language=target.get("language"),
            variables={
                "event_type": context.event_type,
                "event_id": context.event_id,
                **(context.payload if isinstance(context.payload, dict) else {}),
            },
            source_service="webhooks",
        )
    except ValueError as exc:
        # A malformed request is an authoring mistake in the subscription;
        # repeating it produces the same ValueError forever.
        return DeliveryResult(ok=False, retryable=False, detail=f"invalid request: {exc}")
    if queued:
        return DeliveryResult(ok=True)
    return DeliveryResult(ok=False, retryable=True, detail="notification bus refused the request")


# ── ws ───────────────────────────────────────────────────────────────


def deliver_ws(context: DeliveryContext) -> DeliveryResult:
    """Emit an ephemeral frame on a realtime stream (comm Signal).

    A Signal is at-most-once by contract: it is "show this to a live
    observer, if one is watching", and losing it is correct — the truth
    stays in the emitting module's DB behind REST. So a ws delivery that
    reached the transport counts as delivered even if nobody had the socket
    open. Retrying it would replay yesterday's toast at whoever connects
    next, which is worse than the miss.
    """
    from stapel_core.comm import signal
    from stapel_core.comm.exceptions import SignalError

    target = context.target
    try:
        signal(
            target["stream"],
            target.get("frame_type") or context.event_type,
            context.payload if isinstance(context.payload, dict) else {},
        )
    except SignalError as exc:
        # A malformed stream key or a reserved frame type is authoring, not
        # weather: the same call fails identically forever.
        return DeliveryResult(ok=False, retryable=False, detail=f"{type(exc).__name__}: {exc}")
    return DeliveryResult(ok=True)


# ── custom ───────────────────────────────────────────────────────────


def validate_custom_target(target: dict) -> None:
    """Refuse a ``custom`` target whose path is not allowlisted.

    ``ALLOWED_CUSTOM_PATHS`` ships empty, so out of the box no path is
    accepted at all. That is the point: a dotted path stored in a row is
    in-process code chosen by data, and "any authenticated user may name a
    callable" is not a seam, it is remote code execution with extra steps.
    A host that wants the type lists its handlers, by name, once.
    """
    from .conf import webhooks_settings
    from .registry import InvalidTarget

    allowed = [str(p) for p in (webhooks_settings.ALLOWED_CUSTOM_PATHS or ())]
    path = str(target.get("path") or "")
    if not allowed:
        raise InvalidTarget(
            "the custom delivery type has no allowlisted handlers "
            "(STAPEL_WEBHOOKS['ALLOWED_CUSTOM_PATHS'] is empty)"
        )
    if path not in allowed:
        raise InvalidTarget(f"handler {path!r} is not in STAPEL_WEBHOOKS['ALLOWED_CUSTOM_PATHS']")


def deliver_custom(context: DeliveryContext) -> DeliveryResult:
    """Call the app-layer handler the subscription names.

    The allowlist is re-checked here, not only at subscription time: a path
    that was allowlisted when the row was written and is not any more must
    stop being called, and the row outlives the setting.

    A handler may return a :class:`DeliveryResult` to control the ladder
    itself; anything else is read as "done" (``None`` included — a handler
    that returns nothing and did not raise did its job).
    """
    from django.utils.module_loading import import_string

    from .registry import InvalidTarget

    try:
        validate_custom_target(context.target)
    except InvalidTarget as exc:
        return DeliveryResult(ok=False, retryable=False, detail=str(exc))
    try:
        handler = import_string(str(context.target["path"]))
    except ImportError as exc:
        return DeliveryResult(ok=False, retryable=False, detail=f"unimportable handler: {exc}")
    try:
        result = handler(context)
    except Exception as exc:  # noqa: BLE001 — a host handler may raise anything
        logger.exception("custom webhook handler %s failed", context.target.get("path"))
        return DeliveryResult(ok=False, retryable=True, detail=f"{type(exc).__name__}: {exc}")
    if isinstance(result, DeliveryResult):
        return result
    return DeliveryResult(ok=True)


__all__ = [
    "DeliveryContext",
    "DeliveryResult",
    "deliver_custom",
    "deliver_notification",
    "deliver_webhook",
    "deliver_ws",
    "encode_body",
    "envelope",
    "validate_custom_target",
]
