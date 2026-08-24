"""DRF views for stapel-webhooks.

Access model, in two layers.

The gate is ``HasWorkspaceMandateIfScoped`` — the library-shaped mandate
check: in a deployment that can answer the mandate question it enforces the
third principal state (a registered account belonging to no workspace is a
guest, not a user), and in a single-tenant deployment, where no mandate
exists for anybody to hold, it admits. The strict class would 503 everyone
in the second shape, and the reaction layer must be installable there.

The scope is **ownership**: a caller sees and edits the rules whose
``owner_id`` is their user id; staff see all. There is no per-workspace
capability call, which is a choice rather than an omission — a fail-closed
capability check against a service that is not installed would make the
module unusable in exactly the hosts most likely to want it.
``workspace_id`` is carried on the row for hosts that do have tenancy, and
scoping by it is one subclassed view away.

Presenter-canonical (§55): a view resolves its presenter through
``get_presenter`` and returns ``StapelResponse(Serializer(presenter.present(
...)))`` — it never instantiates a ``dto.py`` dataclass itself (SWAP002)
and never imports the concrete presenter class (SWAP001).
"""
from __future__ import annotations

import functools

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.views import APIView
from stapel_core.django.api.errors import StapelErrorResponse, StapelResponse
from stapel_core.django.api.permissions import HasWorkspaceMandateIfScoped

from . import services
from .catalog import event_catalog
from .conf import webhooks_settings
from .errors import ERR_403_FORBIDDEN, ERR_404_DELIVERY, ERR_404_SUBSCRIPTION
from .models import Delivery, Subscription
from .presenters import (
    get_delivery_presenter,
    get_subscription_presenter,
    present_catalog,
    present_replay,
    present_secret,
)
from .registry import get_delivery_types
from .serializers import (
    DeliveryListQuerySerializer,
    DeliverySerializer,
    EventCatalogSerializer,
    ReplayResultSerializer,
    SubscriptionCreateSerializer,
    SubscriptionListQuerySerializer,
    SubscriptionPatchSerializer,
    SubscriptionSecretSerializer,
    SubscriptionSerializer,
)


class SerializerSeamMixin:
    """Overridable serializer seam for every stapel-webhooks APIView.

    Host projects swap the request/response serializer of any view by
    subclassing and setting ``request_serializer_class`` /
    ``response_serializer_class`` — no need to rewrite the method bodies.
    """

    request_serializer_class = None
    response_serializer_class = None

    def get_request_serializer_class(self):
        return self.request_serializer_class

    def get_response_serializer_class(self):
        return self.response_serializer_class


def _maps_webhooks_errors(method):
    """Translate service refusals into the unified error envelope."""

    @functools.wraps(method)
    def wrapper(self, request, *args, **kwargs):
        try:
            return method(self, request, *args, **kwargs)
        except services.WebhooksError as exc:
            return StapelErrorResponse(exc.status, exc.error_key, exc.params)

    return wrapper


def _is_staff(request) -> bool:
    user = getattr(request, "user", None)
    return bool(getattr(user, "is_staff", False) or getattr(user, "is_superuser", False))


def _owner_id(request):
    return getattr(getattr(request, "user", None), "pk", None)


def _page_size(requested) -> int:
    cap = int(webhooks_settings.MAX_PAGE_SIZE or 100)
    if not requested:
        return cap
    return min(int(requested), cap)


def _scoped_subscription(request, subscription_id):
    """``(subscription, error_response)`` — 404 for a stranger's row.

    Not 403: the id of a subscription is not public, and answering "exists
    but not yours" turns the endpoint into an enumeration oracle for other
    tenants' rule ids.
    """
    row = Subscription.objects.filter(pk=subscription_id).first()
    if row is None:
        return None, StapelErrorResponse(404, ERR_404_SUBSCRIPTION)
    if _is_staff(request):
        return row, None
    if row.owner_id and str(row.owner_id) == str(_owner_id(request)):
        return row, None
    if row.owner_id is None:
        # An unowned rule (created in code, or by a deleted account) is an
        # operator's object, never a user's.
        return None, StapelErrorResponse(403, ERR_403_FORBIDDEN)
    return None, StapelErrorResponse(404, ERR_404_SUBSCRIPTION)


_LIMIT_PARAM = OpenApiParameter(
    name="limit", type=int, location=OpenApiParameter.QUERY, required=False
)


@extend_schema(tags=["Webhooks"])
class SubscriptionListCreateView(SerializerSeamMixin, APIView):
    """List the caller's reaction rules, or write a new one."""

    permission_classes = [HasWorkspaceMandateIfScoped]
    request_serializer_class = SubscriptionCreateSerializer
    response_serializer_class = SubscriptionSerializer

    @extend_schema(
        parameters=[
            _LIMIT_PARAM,
            OpenApiParameter(name="event_type", type=str, location=OpenApiParameter.QUERY, required=False),
            OpenApiParameter(name="is_active", type=bool, location=OpenApiParameter.QUERY, required=False),
        ],
        responses={200: SubscriptionSerializer(many=True)},
    )
    @_maps_webhooks_errors
    def get(self, request):
        query = SubscriptionListQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        rows = Subscription.objects.all()
        if not _is_staff(request):
            rows = rows.filter(owner_id=_owner_id(request))
        if query.validated_data.get("event_type"):
            rows = rows.filter(event_type=query.validated_data["event_type"])
        if query.validated_data.get("is_active") is not None:
            rows = rows.filter(is_active=query.validated_data["is_active"])
        presenter = get_subscription_presenter()
        rows = rows[: _page_size(query.validated_data.get("limit"))]
        return StapelResponse(
            self.get_response_serializer_class()(
                [presenter.present(row) for row in rows], many=True
            ).data
        )

    @extend_schema(request=SubscriptionCreateSerializer, responses={201: SubscriptionSecretSerializer})
    @_maps_webhooks_errors
    def post(self, request):
        body = self.get_request_serializer_class()(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data
        subscription = services.create_subscription(
            event_type=data["event_type"],
            delivery=data["delivery"],
            target=data["target"],
            payload_filter=data.get("filter") or {},
            owner_id=_owner_id(request),
            workspace_id=data.get("workspace_id"),
            description=data.get("description", ""),
            created_by=getattr(request, "user", None),
        )
        # The secret envelope, not the rule: this is the only response that
        # ever carries it.
        return StapelResponse(
            SubscriptionSecretSerializer(present_secret(subscription)).data, status=201
        )


@extend_schema(tags=["Webhooks"])
class SubscriptionDetailView(SerializerSeamMixin, APIView):
    """Read, patch or delete one reaction rule."""

    permission_classes = [HasWorkspaceMandateIfScoped]
    request_serializer_class = SubscriptionPatchSerializer
    response_serializer_class = SubscriptionSerializer

    @extend_schema(responses={200: SubscriptionSerializer})
    @_maps_webhooks_errors
    def get(self, request, subscription_id):
        row, denied = _scoped_subscription(request, subscription_id)
        if denied:
            return denied
        return StapelResponse(
            self.get_response_serializer_class()(get_subscription_presenter().present(row)).data
        )

    @extend_schema(request=SubscriptionPatchSerializer, responses={200: SubscriptionSerializer})
    @_maps_webhooks_errors
    def patch(self, request, subscription_id):
        row, denied = _scoped_subscription(request, subscription_id)
        if denied:
            return denied
        body = self.get_request_serializer_class()(data=request.data, partial=True)
        body.is_valid(raise_exception=True)
        changes = dict(body.validated_data)
        if "filter" in changes:
            changes["payload_filter"] = changes.pop("filter")
        services.update_subscription(row, **changes)
        return StapelResponse(
            self.get_response_serializer_class()(get_subscription_presenter().present(row)).data
        )

    @extend_schema(responses={204: None})
    @_maps_webhooks_errors
    def delete(self, request, subscription_id):
        row, denied = _scoped_subscription(request, subscription_id)
        if denied:
            return denied
        row.delete()
        return StapelResponse(None, status=204)


@extend_schema(tags=["Webhooks"])
class SubscriptionSecretView(SerializerSeamMixin, APIView):
    """Rotate the signing secret and hand back the new one."""

    permission_classes = [HasWorkspaceMandateIfScoped]
    response_serializer_class = SubscriptionSecretSerializer

    @extend_schema(request=None, responses={200: SubscriptionSecretSerializer})
    @_maps_webhooks_errors
    def post(self, request, subscription_id):
        row, denied = _scoped_subscription(request, subscription_id)
        if denied:
            return denied
        services.rotate_secret(row)
        return StapelResponse(
            self.get_response_serializer_class()(present_secret(row)).data
        )


@extend_schema(tags=["Webhooks"])
class SubscriptionDeliveryListView(SerializerSeamMixin, APIView):
    """The delivery log of one rule — including its dead letters."""

    permission_classes = [HasWorkspaceMandateIfScoped]
    response_serializer_class = DeliverySerializer

    @extend_schema(
        parameters=[
            _LIMIT_PARAM,
            OpenApiParameter(name="status", type=str, location=OpenApiParameter.QUERY, required=False),
        ],
        responses={200: DeliverySerializer(many=True)},
    )
    @_maps_webhooks_errors
    def get(self, request, subscription_id):
        row, denied = _scoped_subscription(request, subscription_id)
        if denied:
            return denied
        query = DeliveryListQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        rows = row.deliveries.all()
        if query.validated_data.get("status"):
            rows = rows.filter(status=query.validated_data["status"])
        presenter = get_delivery_presenter()
        rows = rows[: _page_size(query.validated_data.get("limit"))]
        return StapelResponse(
            self.get_response_serializer_class()(
                [presenter.present(item) for item in rows], many=True
            ).data
        )


def _scoped_delivery(request, delivery_id):
    row = Delivery.objects.filter(pk=delivery_id).select_related("subscription").first()
    if row is None:
        return None, StapelErrorResponse(404, ERR_404_DELIVERY)
    _, denied = _scoped_subscription(request, row.subscription_id)
    if denied:
        return None, StapelErrorResponse(404, ERR_404_DELIVERY)
    return row, None


@extend_schema(tags=["Webhooks"])
class DeliveryDetailView(SerializerSeamMixin, APIView):
    """One delivery record, payload included — what a replay would send."""

    permission_classes = [HasWorkspaceMandateIfScoped]
    response_serializer_class = DeliverySerializer

    @extend_schema(responses={200: DeliverySerializer})
    @_maps_webhooks_errors
    def get(self, request, delivery_id):
        row, denied = _scoped_delivery(request, delivery_id)
        if denied:
            return denied
        return StapelResponse(
            self.get_response_serializer_class()(get_delivery_presenter().present(row)).data
        )


@extend_schema(tags=["Webhooks"])
class DeliveryReplayView(SerializerSeamMixin, APIView):
    """Put a dead letter back in the queue, with the full ladder again."""

    permission_classes = [HasWorkspaceMandateIfScoped]
    response_serializer_class = ReplayResultSerializer

    @extend_schema(request=None, responses={200: ReplayResultSerializer})
    @_maps_webhooks_errors
    def post(self, request, delivery_id):
        row, denied = _scoped_delivery(request, delivery_id)
        if denied:
            return denied
        services.replay(row)
        return StapelResponse(
            self.get_response_serializer_class()(present_replay(row)).data
        )


@extend_schema(tags=["Webhooks"])
class EventCatalogView(SerializerSeamMixin, APIView):
    """What this deployment can react to, and how it can deliver.

    Generated from installed packages' ``schemas/emits/`` on every call
    (the scan is cached per process), so the subscription builder is never
    a mirror of a list somebody maintains.
    """

    permission_classes = [HasWorkspaceMandateIfScoped]
    response_serializer_class = EventCatalogSerializer

    @extend_schema(responses={200: EventCatalogSerializer})
    @_maps_webhooks_errors
    def get(self, request):
        from .actions import watched_events

        watched = watched_events()
        catalog = {
            name: entry for name, entry in event_catalog().items() if name in watched
        }
        # A watched topic with no shipped schema is still subscribable; it
        # appears with an empty shape rather than being hidden, because
        # hiding it reads as "this event does not exist".
        for name in sorted(watched - set(catalog)):
            catalog[name] = {
                "event": name,
                "module": "",
                "package": "",
                "description": "Declared by STAPEL_WEBHOOKS['WATCH_EVENTS']; no schema shipped.",
                "required": [],
                "properties": [],
            }
        return StapelResponse(
            self.get_response_serializer_class()(
                present_catalog(catalog, get_delivery_types())
            ).data
        )


__all__ = [
    "DeliveryDetailView",
    "DeliveryReplayView",
    "EventCatalogView",
    "SerializerSeamMixin",
    "SubscriptionDeliveryListView",
    "SubscriptionDetailView",
    "SubscriptionListCreateView",
    "SubscriptionSecretView",
]
