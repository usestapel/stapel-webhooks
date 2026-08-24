"""v1 URL set — paths here are relative to the ``api/v1/`` mount contributed
by the root ``urls.py`` (api-versioning.md §2).

There is no public (anonymous) surface. A reaction layer has no anonymous
verb: creating a rule means naming a destination this service will dial, and
reading one means reading other modules' payloads.
"""
from typing import NamedTuple

from django.urls import path

from .errors import WebhooksErrorKeysView
from .views import (
    DeliveryDetailView,
    DeliveryReplayView,
    EventCatalogView,
    SubscriptionDeliveryListView,
    SubscriptionDetailView,
    SubscriptionListCreateView,
    SubscriptionSecretView,
)

urlpatterns = [
    # The subscription builder's vocabulary, generated from schemas/emits.
    path("event-catalog", EventCatalogView.as_view(), name="webhooks-event-catalog"),
    path("subscriptions", SubscriptionListCreateView.as_view(), name="webhooks-subscriptions"),
    path(
        "subscriptions/<uuid:subscription_id>",
        SubscriptionDetailView.as_view(),
        name="webhooks-subscription-detail",
    ),
    path(
        "subscriptions/<uuid:subscription_id>/secret",
        SubscriptionSecretView.as_view(),
        name="webhooks-subscription-secret",
    ),
    path(
        "subscriptions/<uuid:subscription_id>/deliveries",
        SubscriptionDeliveryListView.as_view(),
        name="webhooks-subscription-deliveries",
    ),
    path("deliveries/<uuid:delivery_id>", DeliveryDetailView.as_view(), name="webhooks-delivery"),
    path(
        "deliveries/<uuid:delivery_id>/replay",
        DeliveryReplayView.as_view(),
        name="webhooks-delivery-replay",
    ),
    # The listing the stapel-translate error collector reads.
    path("error-keys/", WebhooksErrorKeysView.as_view(), name="webhooks-error-keys"),
]


class GateEntry(NamedTuple):
    """One gated URL block (capability-config.md §2 p.2). ``flags`` compose
    with OR; empty flags = always on."""

    name: str
    flags: tuple
    patterns: tuple


#: webhooks has no per-method config gates: closing a delivery type is a
#: registry decision (``DELIVERY_TYPES = {"custom": None}``) that the create
#: endpoint enforces, not a route that disappears. Declared as a registry
#: entry anyway so the capabilities.json emitter has a uniform mechanism.
GATE_REGISTRY: dict = {
    "webhooks.api": GateEntry("webhooks.api", (), tuple(urlpatterns)),
}
