"""Emitted actions of stapel-webhooks (transactional outbox, at-least-once).

The reaction layer is itself an event source, and it has to be: a delivery
that dies is an operational fact somebody must learn about, and the only
honest channel for it is the one every other fact in the fleet travels on.

**Ids and outcomes only, never the delivered payload.** The payload already
travelled once, to the subscriber that was entitled to it; putting it on the
bus again would fan a subscriber's data out to every consumer of these
topics. A consumer that needs the body reads the delivery row under the
staff mandate.

These three topics are in ``IGNORE_EVENTS`` by default (``conf.py``). A
subscription on ``webhooks.delivery.dead`` whose own delivery dies emits
another one, and the loop is bounded only by the retry ladder — so the
default is that the reaction layer does not react to itself. A host that
genuinely wants a delivery-failure webhook removes the entry, knowingly.
"""
from __future__ import annotations

from stapel_core.comm import emit

DELIVERY_SUCCEEDED = "webhooks.delivery.succeeded"
DELIVERY_DEAD = "webhooks.delivery.dead"
SUBSCRIPTION_DISABLED = "webhooks.subscription.disabled"

#: Every topic this module emits — the ``IGNORE_EVENTS`` default and the
#: self-reference check both read it instead of repeating the list.
EMITTED_EVENTS = (DELIVERY_SUCCEEDED, DELIVERY_DEAD, SUBSCRIPTION_DISABLED)


def emit_delivery_succeeded(delivery) -> None:
    emit(
        DELIVERY_SUCCEEDED,
        {
            "delivery_id": str(delivery.id),
            "subscription_id": str(delivery.subscription_id),
            "event_type": delivery.event_type,
            "event_id": delivery.event_id or "",
            "attempts": int(delivery.attempts),
        },
        key=str(delivery.subscription_id),
    )


def emit_delivery_dead(delivery) -> None:
    emit(
        DELIVERY_DEAD,
        {
            "delivery_id": str(delivery.id),
            "subscription_id": str(delivery.subscription_id),
            "event_type": delivery.event_type,
            "event_id": delivery.event_id or "",
            "attempts": int(delivery.attempts),
            "error": (delivery.last_error or "")[:500],
        },
        key=str(delivery.subscription_id),
    )


def emit_subscription_disabled(subscription, reason: str) -> None:
    emit(
        SUBSCRIPTION_DISABLED,
        {
            "subscription_id": str(subscription.id),
            "event_type": subscription.event_type,
            "delivery": subscription.delivery,
            "reason": reason,
        },
        key=str(subscription.id),
    )


__all__ = [
    "DELIVERY_DEAD",
    "DELIVERY_SUCCEEDED",
    "EMITTED_EVENTS",
    "SUBSCRIPTION_DISABLED",
    "emit_delivery_dead",
    "emit_delivery_succeeded",
    "emit_subscription_disabled",
]
