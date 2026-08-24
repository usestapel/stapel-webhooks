"""Presenters for stapel-webhooks — the DTO-building layer (§55).

Presenter discipline (enforced by SWAP001/SWAP002 in ``stapel-verify``):
views NEVER instantiate a ``dto.py`` dataclass directly — every DTO is built
by a presenter resolved through ``get_presenter(KEY, default=...)``, so a
host project can reshape any envelope via ``STAPEL_SWAP`` without forking
this module.

One field is absent from :class:`SubscriptionPresenter` on purpose and must
stay absent: ``secret``. It is returned exactly twice in a subscription's
life — by the call that created it and by the call that rotated it — through
:class:`SubscriptionSecretDTO`. Adding it here would put a signing key in
every list response, every log of one, and every cache in front of it.
"""
from __future__ import annotations

from typing import Optional

from stapel_core.django.api.presenters import Presenter, PresenterField
from stapel_core.django.swappable import declare_swap, get_presenter

from .dto import CatalogEventDTO, EventCatalogDTO, ReplayResultDTO, SubscriptionSecretDTO
from .models import Delivery, Subscription

SUBSCRIPTION_PRESENTER_KEY = "WEBHOOKS_SUBSCRIPTION_PRESENTER"
DEFAULT_SUBSCRIPTION_PRESENTER = "stapel_webhooks.presenters.SubscriptionPresenter"
DELIVERY_PRESENTER_KEY = "WEBHOOKS_DELIVERY_PRESENTER"
DEFAULT_DELIVERY_PRESENTER = "stapel_webhooks.presenters.DeliveryPresenter"

declare_swap(SUBSCRIPTION_PRESENTER_KEY, DEFAULT_SUBSCRIPTION_PRESENTER)
declare_swap(DELIVERY_PRESENTER_KEY, DEFAULT_DELIVERY_PRESENTER)


def _iso(value):
    return value.isoformat() if value else None


class SubscriptionPresenter(Presenter):
    """Presents a reaction rule to its owner.

    Example:
        {
            "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
            "event_type": "listing.published",
            "delivery": "webhook",
            "target": {"url": "https://example.com/hooks/stapel"},
            "filter": {"city": "berlin"},
            "description": "CRM sync",
            "is_active": true,
            "has_secret": true,
            "consecutive_failures": 0,
            "disabled_at": null,
            "last_delivery_at": "2026-08-24T10:00:00+00:00",
            "owner_id": "5cc26b64-0717-4562-b3fc-2c963f66a001",
            "workspace_id": null,
            "created_at": "2026-08-20T09:00:00+00:00",
            "updated_at": "2026-08-24T10:00:00+00:00"
        }
    """

    model = Subscription
    fields = ("event_type", "delivery", "description", "is_active", "consecutive_failures")
    custom_fields = {
        "id": PresenterField(type=str, source=lambda dao: str(dao.id)),
        # JSONField passthrough deduces to ``Any``, which the dataclass
        # serializer refuses to build a field for — the shape is declared
        # here instead of left to inference.
        "target": PresenterField(
            type=dict,
            source=lambda dao: dao.target or {},
            help_text="Delivery-type-specific destination.",
        ),
        "filter": PresenterField(
            type=dict,
            source=lambda dao: dao.payload_filter or {},
            help_text="JSON predicate over the event payload; {} matches every event.",
        ),
        "has_secret": PresenterField(
            type=bool,
            source=lambda dao: bool(dao.secret),
            help_text="Whether a signing secret exists. The secret itself is only ever returned by create and rotate.",
        ),
        "owner_id": PresenterField(
            type=Optional[str],
            source=lambda dao: str(dao.owner_id) if dao.owner_id else None,
            default=None,
        ),
        "workspace_id": PresenterField(
            type=Optional[str],
            source=lambda dao: str(dao.workspace_id) if dao.workspace_id else None,
            default=None,
        ),
        "disabled_at": PresenterField(
            type=Optional[str], source=lambda dao: _iso(dao.disabled_at), default=None
        ),
        "last_delivery_at": PresenterField(
            type=Optional[str], source=lambda dao: _iso(dao.last_delivery_at), default=None
        ),
        "created_at": PresenterField(type=Optional[str], source=lambda dao: _iso(dao.created_at), default=None),
        "updated_at": PresenterField(type=Optional[str], source=lambda dao: _iso(dao.updated_at), default=None),
    }


class DeliveryPresenter(Presenter):
    """Presents one delivery attempt record.

    Example:
        {
            "id": "9aa1c0de-0000-4000-8000-000000000001",
            "subscription_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
            "event_type": "listing.published",
            "event_id": "b0c1...",
            "status": "retrying",
            "attempts": 2,
            "response_status": 503,
            "last_error": "Service Unavailable",
            "next_attempt_at": "2026-08-24T10:05:00+00:00",
            "last_attempt_at": "2026-08-24T10:00:00+00:00",
            "completed_at": null,
            "created_at": "2026-08-24T09:59:00+00:00"
        }
    """

    model = Delivery
    fields = ("event_type", "event_id", "status", "attempts", "response_status", "last_error")
    custom_fields = {
        "id": PresenterField(type=str, source=lambda dao: str(dao.id)),
        "subscription_id": PresenterField(type=str, source=lambda dao: str(dao.subscription_id)),
        "payload": PresenterField(
            type=dict,
            source=lambda dao: dao.payload or {},
            help_text="The event payload as it was matched — what a replay would send.",
        ),
        "next_attempt_at": PresenterField(type=Optional[str], source=lambda dao: _iso(dao.next_attempt_at), default=None),
        "last_attempt_at": PresenterField(type=Optional[str], source=lambda dao: _iso(dao.last_attempt_at), default=None),
        "completed_at": PresenterField(type=Optional[str], source=lambda dao: _iso(dao.completed_at), default=None),
        "created_at": PresenterField(type=Optional[str], source=lambda dao: _iso(dao.created_at), default=None),
    }


def get_subscription_presenter():
    """The active (possibly host-swapped) subscription presenter."""
    return get_presenter(SUBSCRIPTION_PRESENTER_KEY, default=DEFAULT_SUBSCRIPTION_PRESENTER)


def get_delivery_presenter():
    """The active (possibly host-swapped) delivery presenter."""
    return get_presenter(DELIVERY_PRESENTER_KEY, default=DEFAULT_DELIVERY_PRESENTER)


def present_secret(subscription) -> SubscriptionSecretDTO:
    """The create/rotate envelope — the only shape carrying the secret."""
    return SubscriptionSecretDTO(id=str(subscription.id), secret=subscription.secret or "")


def present_catalog(catalog: dict, delivery_types) -> EventCatalogDTO:
    """The subscription builder's vocabulary."""
    return EventCatalogDTO(
        events=[
            CatalogEventDTO(
                event=entry["event"],
                module=entry["module"],
                package=entry["package"],
                description=entry["description"],
                required=list(entry["required"]),
                properties=list(entry["properties"]),
            )
            for entry in sorted(catalog.values(), key=lambda e: e["event"])
        ],
        delivery_types=sorted(delivery_types),
    )


def present_replay(delivery) -> ReplayResultDTO:
    return ReplayResultDTO(
        id=str(delivery.id),
        status=delivery.status,
        next_attempt_at=_iso(delivery.next_attempt_at),
    )


__all__ = [
    "DEFAULT_DELIVERY_PRESENTER",
    "DEFAULT_SUBSCRIPTION_PRESENTER",
    "DELIVERY_PRESENTER_KEY",
    "SUBSCRIPTION_PRESENTER_KEY",
    "DeliveryPresenter",
    "SubscriptionPresenter",
    "get_delivery_presenter",
    "get_subscription_presenter",
    "present_catalog",
    "present_replay",
    "present_secret",
]
