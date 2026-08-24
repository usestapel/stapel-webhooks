"""The reaction layer itself: match, plan, attempt, retry, dead-letter.

Reading order:

1. :func:`dispatch_event` — one emitted comm Action in, N ``Delivery`` rows
   out. This is the matcher: active subscriptions for the event type, each
   one's filter evaluated against the payload.
2. :func:`attempt` — one row, one try, one state transition. The whole
   retry/dead-letter machine is here and nowhere else.
3. :func:`drain` — the loop a scheduler runs over what is due.

The invariant the split exists to protect: **matching is cheap and
transactional, delivering is slow and external.** Matching runs where the
event lands, inside the emitting process, and only writes rows. Delivering
dials strangers, and by default (``DISPATCH_MODE = "deferred"``) it happens
somewhere else entirely. A deployment that sets ``"inline"`` accepts a
receiver's latency on its own critical path, knowingly, and gets a system
check reminding it.

The subscription-authoring half (create/update/rotate) lives here too, so
the validation that decides what a rule may say has exactly one home — the
REST views and a host calling in Python go through the same door.
"""
from __future__ import annotations

import logging
import random
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from . import events
from .conf import webhooks_settings
from .deliveries import DeliveryContext, DeliveryResult
from .errors import (
    ERR_400_INSECURE_TARGET,
    ERR_400_INVALID_FILTER,
    ERR_400_INVALID_TARGET,
    ERR_400_NOT_SIGNED_TYPE,
    ERR_400_UNKNOWN_DELIVERY,
    ERR_400_UNKNOWN_EVENT,
    ERR_409_NOT_REPLAYABLE,
    ERR_409_SUBSCRIPTION_CAP,
)
from .filters import InvalidFilter, validate_filter
from .models import (
    DUE_STATUSES,
    STATUS_DEAD,
    STATUS_PENDING,
    STATUS_RETRYING,
    STATUS_SUCCEEDED,
    Delivery,
    Subscription,
)
from .registry import (
    InvalidTarget,
    UnknownDeliveryType,
    delivery_handler,
    is_signed,
    resolve_delivery,
    validate_target,
)
from .signing import generate_secret

logger = logging.getLogger(__name__)


class WebhooksError(Exception):
    """A refusal with an HTTP status and an i18n error key."""

    def __init__(self, status: int, error_key: str, params: dict | None = None) -> None:
        super().__init__(error_key)
        self.status = status
        self.error_key = error_key
        self.params = params or {}


# ─────────────────────────────────────────────────────────────────────
# Subscription authoring
# ─────────────────────────────────────────────────────────────────────


def validate_subscription(
    *, event_type: str, delivery: str, target: dict, payload_filter: dict | None
) -> None:
    """Everything a rule must satisfy before it is written.

    Refusing here rather than at delivery time is the whole reason this
    function exists: a target the delivery type cannot use, or a filter the
    matcher cannot evaluate, would otherwise surface as a dead letter
    discovered by whoever was waiting for the reaction.
    """
    from .actions import watched_events

    if event_type not in watched_events():
        raise WebhooksError(400, ERR_400_UNKNOWN_EVENT, {"event_type": event_type})
    try:
        resolve_delivery(delivery)
    except UnknownDeliveryType:
        raise WebhooksError(400, ERR_400_UNKNOWN_DELIVERY, {"delivery": delivery}) from None
    try:
        validate_target(delivery, target)
    except InvalidTarget as exc:
        raise WebhooksError(400, ERR_400_INVALID_TARGET, {"detail": str(exc)}) from None
    if delivery == "webhook" and not webhooks_settings.ALLOW_INSECURE_TARGETS:
        url = str(target.get("url") or "")
        if not url.lower().startswith("https://"):
            raise WebhooksError(400, ERR_400_INSECURE_TARGET, {"url": url})
    try:
        validate_filter(payload_filter)
    except InvalidFilter as exc:
        raise WebhooksError(400, ERR_400_INVALID_FILTER, {"detail": str(exc)}) from None


def create_subscription(
    *,
    event_type: str,
    delivery: str,
    target: dict,
    payload_filter: dict | None = None,
    owner_id=None,
    workspace_id=None,
    description: str = "",
    created_by=None,
    secret: str | None = None,
) -> Subscription:
    """Write one reaction rule. Generates the signing secret when the
    delivery type is a signed one and the caller supplied none."""
    validate_subscription(
        event_type=event_type,
        delivery=delivery,
        target=target,
        payload_filter=payload_filter,
    )
    cap = int(webhooks_settings.MAX_SUBSCRIPTIONS_PER_OWNER or 0)
    if cap and owner_id is not None:
        if Subscription.objects.filter(owner_id=owner_id).count() >= cap:
            raise WebhooksError(409, ERR_409_SUBSCRIPTION_CAP, {"limit": cap})
    if secret is None:
        secret = generate_secret() if is_signed(delivery) else ""
    return Subscription.objects.create(
        event_type=event_type,
        delivery=delivery,
        target=target,
        payload_filter=payload_filter or {},
        owner_id=owner_id,
        workspace_id=workspace_id,
        description=description or "",
        created_by=created_by,
        secret=secret,
    )


def update_subscription(subscription: Subscription, **changes) -> Subscription:
    """Patch a rule. Re-validates the whole rule, not the changed field:
    a target is only valid *for* a delivery type, and a filter is only
    bounded relative to the current settings."""
    event_type = changes.get("event_type", subscription.event_type)
    delivery = changes.get("delivery", subscription.delivery)
    target = changes.get("target", subscription.target)
    payload_filter = changes.get("payload_filter", subscription.payload_filter)
    validate_subscription(
        event_type=event_type,
        delivery=delivery,
        target=target,
        payload_filter=payload_filter,
    )
    subscription.event_type = event_type
    subscription.delivery = delivery
    subscription.target = target
    subscription.payload_filter = payload_filter or {}
    if "description" in changes:
        subscription.description = changes["description"] or ""
    if "is_active" in changes:
        subscription.is_active = bool(changes["is_active"])
        if subscription.is_active:
            # Re-activating clears the strike count: the operator has looked
            # at it, and carrying old strikes would disable it again on the
            # first blip.
            subscription.consecutive_failures = 0
            subscription.disabled_at = None
    subscription.save()
    return subscription


def rotate_secret(subscription: Subscription) -> str:
    """Issue a new signing secret and return it (the only time it is
    readable). An unsigned delivery type has nothing to rotate, and saying
    so is more useful than handing back an unused string."""
    if not is_signed(subscription.delivery):
        raise WebhooksError(400, ERR_400_NOT_SIGNED_TYPE, {"delivery": subscription.delivery})
    subscription.secret = generate_secret()
    subscription.save(update_fields=["secret", "updated_at"])
    return subscription.secret


# ─────────────────────────────────────────────────────────────────────
# Matching and planning
# ─────────────────────────────────────────────────────────────────────


def matching_subscriptions(event_type: str, payload: dict):
    """Active rules for *event_type* whose filter accepts *payload*.

    The matcher is the ``MATCHER`` seam, resolved per call so a host can
    swap the predicate language without this module knowing.
    """
    matcher = webhooks_settings.MATCHER
    rows = Subscription.objects.filter(event_type=event_type, is_active=True)
    out = []
    for subscription in rows.iterator():
        try:
            if matcher(payload, subscription.payload_filter):
                out.append(subscription)
        except Exception:  # noqa: BLE001 — one bad predicate must not silence the rest
            logger.exception(
                "webhooks: filter of subscription %s raised; treated as no match",
                subscription.id,
            )
    return out


def idempotency_key(event_id: str, subscription_id) -> str:
    """``<event_id>:<subscription_id>`` — the at-least-once seam.

    Action delivery is at-least-once by contract, so the same event reaches
    the dispatcher more than once as a matter of course. Uniqueness on this
    column is what makes a redelivery plan nothing instead of duplicating
    every subscriber's webhook.
    """
    return f"{event_id or 'noid'}:{subscription_id}"


def plan_delivery(subscription: Subscription, *, event_type: str, event_id: str, payload: dict):
    """Create (or find) the delivery row for one (event, subscription).

    Returns ``(delivery, created)``. Oversized payloads are dead-lettered at
    planning time rather than pushed at a receiver: the row records why, and
    the operator sees a dead letter instead of a mystery timeout.
    """
    import json

    key = idempotency_key(event_id, subscription.id)
    existing = Delivery.objects.filter(idempotency_key=key).first()
    if existing is not None:
        return existing, False

    limit = int(webhooks_settings.MAX_PAYLOAD_BYTES or 0)
    oversized = False
    if limit:
        try:
            oversized = len(json.dumps(payload, default=str).encode("utf-8")) > limit
        except (TypeError, ValueError):
            oversized = False

    delivery = Delivery(
        subscription=subscription,
        event_type=event_type,
        event_id=event_id or "",
        idempotency_key=key,
        payload=payload if isinstance(payload, dict) else {},
        status=STATUS_DEAD if oversized else STATUS_PENDING,
        next_attempt_at=None if oversized else timezone.now(),
        last_error=f"payload exceeds MAX_PAYLOAD_BYTES ({limit})" if oversized else "",
        completed_at=timezone.now() if oversized else None,
    )
    try:
        delivery.save()
    except Exception:
        # The unique key lost a race with a concurrent dispatcher: that is
        # the mechanism working, not a failure.
        existing = Delivery.objects.filter(idempotency_key=key).first()
        if existing is None:
            raise
        return existing, False
    return delivery, True


def ignored_events() -> set:
    """Topics the reaction layer refuses to react to."""
    return {str(name) for name in (webhooks_settings.IGNORE_EVENTS or ())}


def dispatch_event(event) -> list:
    """Match one emitted comm Action and plan its deliveries.

    Accepts an ``Event`` envelope (what an ``@on_action`` handler receives)
    or anything with ``event_type`` / ``payload`` / ``event_id``. Returns
    the freshly created deliveries — an at-least-once redelivery returns an
    empty list, which is what makes this handler idempotent.
    """
    event_type = getattr(event, "event_type", None) or ""
    if not event_type or event_type in ignored_events():
        return []
    payload = getattr(event, "payload", None) or {}
    event_id = str(getattr(event, "event_id", "") or "")

    planned = []
    for subscription in matching_subscriptions(event_type, payload):
        delivery, created = plan_delivery(
            subscription, event_type=event_type, event_id=event_id, payload=payload
        )
        if created:
            planned.append(delivery)

    if planned and str(webhooks_settings.DISPATCH_MODE or "deferred") == "inline":
        ids = [d.id for d in planned]
        transaction.on_commit(lambda: _attempt_ids(ids))
    return planned


def _attempt_ids(ids) -> None:
    """Deliver a list of rows, never raising into the caller's stack."""
    for delivery_id in ids:
        row = Delivery.objects.filter(pk=delivery_id).first()
        if row is None:
            continue
        try:
            attempt(row)
        except Exception:  # noqa: BLE001 — inline dispatch must not break the emitter
            logger.exception("webhooks: inline attempt of delivery %s failed", delivery_id)


# ─────────────────────────────────────────────────────────────────────
# The attempt / retry / dead-letter machine
# ─────────────────────────────────────────────────────────────────────


def backoff_seconds(attempt_number: int, *, jitter: bool = True) -> float:
    """Exponential backoff with a cap, and optional jitter.

    The cap is what makes the ladder finite in wall-clock terms as well as
    in attempts; the jitter is what stops every delivery queued during one
    outage from re-dialling the recovering receiver in the same second.
    """
    base = float(webhooks_settings.BACKOFF_BASE_SECONDS or 10)
    factor = float(webhooks_settings.BACKOFF_FACTOR or 2)
    cap = float(webhooks_settings.BACKOFF_CAP_SECONDS or 3600)
    delay = min(base * (factor ** max(0, int(attempt_number) - 1)), cap)
    if jitter:
        ratio = float(webhooks_settings.JITTER_RATIO or 0)
        if ratio:
            delay += delay * random.uniform(-ratio, ratio)
    return max(0.0, delay)


def build_context(delivery: Delivery) -> DeliveryContext:
    subscription = delivery.subscription
    return DeliveryContext(
        delivery_id=str(delivery.id),
        subscription_id=str(subscription.id),
        event_type=delivery.event_type,
        event_id=delivery.event_id or "",
        payload=delivery.payload or {},
        target=subscription.target or {},
        secret=subscription.secret or "",
        attempt=int(delivery.attempts) + 1,
        created_at=delivery.created_at,
    )


def attempt(delivery: Delivery) -> Delivery:
    """One try at one delivery, and the state transition it earns.

    Terminal rows are returned untouched — a succeeded delivery re-entering
    the drain (a claim that raced, a replay of the wrong id) must not be
    delivered twice.
    """
    if delivery.is_terminal:
        return delivery

    context = build_context(delivery)
    try:
        handler = delivery_handler(delivery.subscription.delivery)
    except UnknownDeliveryType as exc:
        # The type was removed from the registry after the row was written.
        # Not retryable: the registry is process configuration, and it will
        # answer the same way on every attempt in this process.
        return _apply(delivery, DeliveryResult(ok=False, retryable=False, detail=str(exc)))
    try:
        result = handler(context)
    except Exception as exc:  # noqa: BLE001 — a delivery handler may raise anything
        logger.exception("webhooks: delivery handler failed for %s", delivery.id)
        result = DeliveryResult(ok=False, retryable=True, detail=f"{type(exc).__name__}: {exc}")
    if not isinstance(result, DeliveryResult):
        result = DeliveryResult(ok=True)
    return _apply(delivery, result)


def _apply(delivery: Delivery, result: DeliveryResult) -> Delivery:
    """Write the outcome: status, counters, schedule, and the fact on the bus."""
    now = timezone.now()
    max_attempts = int(webhooks_settings.MAX_ATTEMPTS or 1)
    subscription = delivery.subscription

    with transaction.atomic():
        delivery.attempts = int(delivery.attempts) + 1
        delivery.last_attempt_at = now
        delivery.response_status = result.status_code
        delivery.last_error = (result.detail or "")[:2000]

        if result.ok:
            delivery.status = STATUS_SUCCEEDED
            delivery.completed_at = now
            delivery.next_attempt_at = None
            delivery.last_error = ""
            delivery.save()
            if subscription.consecutive_failures or subscription.last_delivery_at != now:
                subscription.consecutive_failures = 0
                subscription.last_delivery_at = now
                subscription.save(
                    update_fields=["consecutive_failures", "last_delivery_at", "updated_at"]
                )
            events.emit_delivery_succeeded(delivery)
            return delivery

        if result.retryable and delivery.attempts < max_attempts:
            delivery.status = STATUS_RETRYING
            delivery.next_attempt_at = now + timedelta(
                seconds=backoff_seconds(delivery.attempts)
            )
            delivery.save()
            return delivery

        delivery.status = STATUS_DEAD
        delivery.completed_at = now
        delivery.next_attempt_at = None
        delivery.save()
        _register_failure(subscription)
        events.emit_delivery_dead(delivery)
    return delivery


def _register_failure(subscription: Subscription) -> None:
    """Count a dead letter against the rule, and disable it if it keeps
    happening. Caller holds the transaction."""
    threshold = int(webhooks_settings.DISABLE_AFTER_DEAD or 0)
    subscription.consecutive_failures = int(subscription.consecutive_failures) + 1
    fields = ["consecutive_failures", "updated_at"]
    disable = threshold and subscription.consecutive_failures >= threshold and subscription.is_active
    if disable:
        subscription.is_active = False
        subscription.disabled_at = timezone.now()
        fields += ["is_active", "disabled_at"]
    subscription.save(update_fields=fields)
    if disable:
        events.emit_subscription_disabled(  # emit-check: ok — caller (_apply) holds transaction.atomic()
            subscription, f"{subscription.consecutive_failures} consecutive dead-lettered deliveries"
        )


def replay(delivery: Delivery) -> Delivery:
    """Put a dead-lettered delivery back in the queue.

    The attempt counter is reset: a replay is a decision by a human who has
    presumably fixed something, so it gets the full ladder again rather than
    one last try. Only a dead row is replayable — re-queueing a succeeded
    one would deliver the same fact twice on purpose.
    """
    if delivery.status != STATUS_DEAD:
        raise WebhooksError(409, ERR_409_NOT_REPLAYABLE, {"status": delivery.status})
    delivery.status = STATUS_PENDING
    delivery.attempts = 0
    delivery.completed_at = None
    delivery.next_attempt_at = timezone.now()
    delivery.last_error = ""
    delivery.response_status = None
    delivery.save()
    return delivery


# ─────────────────────────────────────────────────────────────────────
# The drain
# ─────────────────────────────────────────────────────────────────────


def due_queryset(now=None):
    """Rows a drain may pick up right now."""
    now = now or timezone.now()
    return Delivery.objects.filter(
        status__in=DUE_STATUSES, next_attempt_at__lte=now
    ).order_by("next_attempt_at", "created_at")


def claim(delivery_id, *, now=None) -> bool:
    """Take exclusive-enough ownership of one row.

    A conditional UPDATE that pushes ``next_attempt_at`` a lease into the
    future: whoever's UPDATE matches a row owns it, everyone else's matches
    zero rows. Works on every backend (``SELECT … SKIP LOCKED`` does not),
    and a worker that dies mid-attempt releases the row by the clock rather
    than by a lock nobody will unlock.
    """
    now = now or timezone.now()
    lease = timedelta(seconds=float(webhooks_settings.CLAIM_LEASE_SECONDS or 120))
    updated = Delivery.objects.filter(
        pk=delivery_id, status__in=DUE_STATUSES, next_attempt_at__lte=now
    ).update(next_attempt_at=now + lease, updated_at=now)
    return bool(updated)


def drain(limit: int | None = None) -> dict:
    """Attempt every due delivery, up to *limit*. Returns the counts.

    This is what the scheduler runs. It never raises for one bad row: a
    delivery that explodes is logged and counted, and the next one is still
    attempted — a drain that dies on the first hostile receiver is a queue
    that never moves again.
    """
    if limit is None:
        limit = int(webhooks_settings.DRAIN_BATCH_SIZE or 100)
    counts = {"attempted": 0, "succeeded": 0, "retrying": 0, "dead": 0, "skipped": 0}
    for delivery_id in list(due_queryset().values_list("pk", flat=True)[:limit]):
        if not claim(delivery_id):
            counts["skipped"] += 1
            continue
        row = Delivery.objects.filter(pk=delivery_id).select_related("subscription").first()
        if row is None:  # pragma: no cover — deleted between claim and read
            counts["skipped"] += 1
            continue
        try:
            attempt(row)
        except Exception:  # noqa: BLE001 — one row must not stop the drain
            logger.exception("webhooks: drain failed on delivery %s", delivery_id)
            counts["attempted"] += 1
            continue
        counts["attempted"] += 1
        if row.status == STATUS_SUCCEEDED:
            counts["succeeded"] += 1
        elif row.status == STATUS_RETRYING:
            counts["retrying"] += 1
        elif row.status == STATUS_DEAD:
            counts["dead"] += 1
    return counts


def purge_deliveries(now=None) -> dict:
    """Drop delivery rows past their retention horizon.

    Succeeded rows are a log and go early; dead rows are evidence and stay
    for the longer horizon. ``None`` on either keeps that class forever.
    """
    now = now or timezone.now()
    counts = {"succeeded": 0, "dead": 0}
    succeeded_days = webhooks_settings.SUCCEEDED_RETENTION_DAYS
    dead_days = webhooks_settings.DEAD_RETENTION_DAYS
    if succeeded_days is not None:
        cutoff = now - timedelta(days=int(succeeded_days))
        counts["succeeded"] = Delivery.objects.filter(
            status=STATUS_SUCCEEDED, completed_at__lt=cutoff
        ).delete()[0]
    if dead_days is not None:
        cutoff = now - timedelta(days=int(dead_days))
        counts["dead"] = Delivery.objects.filter(
            status=STATUS_DEAD, completed_at__lt=cutoff
        ).delete()[0]
    return counts


__all__ = [
    "WebhooksError",
    "attempt",
    "backoff_seconds",
    "build_context",
    "claim",
    "create_subscription",
    "dispatch_event",
    "drain",
    "due_queryset",
    "idempotency_key",
    "ignored_events",
    "matching_subscriptions",
    "plan_delivery",
    "purge_deliveries",
    "replay",
    "rotate_secret",
    "update_subscription",
    "validate_subscription",
]
