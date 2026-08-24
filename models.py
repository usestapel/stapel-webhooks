"""Models of the reaction layer — the rule, and the evidence.

Two tables, and the split between them is the design:

``Subscription`` is the **rule**: *when <event_type> happens, and <filter>
holds, deliver it to <target> by <delivery>*. It is user-facing (§3 of the
design: "event + filter + target = a reaction rule"), long-lived, and
edited by whoever owns it.

``Delivery`` is the **evidence**: one attempt-bearing row per (event,
subscription) pair. It exists so that "we told you" is a fact with a
timestamp, a status code and an error string rather than a log line that
rotated away — and so that a receiver's outage is survivable, because a row
with ``next_attempt_at`` is a promise the drain keeps.

House rules (docs/library-standard.md §3.8): cross-service references are
UUID fields, not FKs; the user model only via ``settings.AUTH_USER_MODEL``;
index/constraint names <= 30 chars.

Staff-mandate declarations (``stapel_core.access``): both models are
``@access.sensitive``. A subscription row carries a signing secret and a
destination URL — together, everything needed to forge this deployment's
webhooks; a delivery row carries a snapshot of an event payload, whose
sensitivity is the emitting module's, not ours. Staff read of either
requires MID clearance, mutation HIGH.
"""
import uuid

from django.conf import settings as django_settings
from django.db import models
from stapel_core.access import access

# ── Delivery lifecycle ───────────────────────────────────────────────
#
# Four states, and the two terminal ones are deliberately different rows in
# a report: SUCCEEDED is "the receiver has it", DEAD is "we gave up and the
# payload is still here". A single "failed" state would merge "will be
# retried in 40 minutes" with "nobody will ever look at this again".
STATUS_PENDING = "pending"
STATUS_RETRYING = "retrying"
STATUS_SUCCEEDED = "succeeded"
STATUS_DEAD = "dead"

DELIVERY_STATUSES = (
    (STATUS_PENDING, "Pending"),
    (STATUS_RETRYING, "Retrying"),
    (STATUS_SUCCEEDED, "Succeeded"),
    (STATUS_DEAD, "Dead-lettered"),
)

#: States a drain pass may pick up.
DUE_STATUSES = (STATUS_PENDING, STATUS_RETRYING)

#: States nothing will move again without a human.
TERMINAL_STATUSES = (STATUS_SUCCEEDED, STATUS_DEAD)


@access.sensitive  # signing secret + destination = forgeable webhooks
class Subscription(models.Model):
    """One reaction rule.

    ``owner_id`` is an FK-less user id (the ``Submission.submitted_by``
    precedent): a subscription outlives the account that made it just long
    enough for an operator to see what was still being delivered, and
    erasure nulls the owner rather than cascading a receiver into silence
    mid-incident. ``workspace_id`` is the tenancy scope when the host has
    one; it is nullable because a single-tenant host has none and a
    mandatory column would be filled with a fiction.
    """

    #: Rule identity. UUID, so an id may be handed to a client without
    #: leaking how many rules the deployment holds.
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    #: FK-less user id of the rule's owner. Null = created in code, or the
    #: account was erased.
    owner_id = models.UUIDField(null=True, blank=True, db_index=True)
    #: Tenancy scope, where the host has one. Null in a single-tenant host.
    workspace_id = models.UUIDField(null=True, blank=True, db_index=True)

    #: The comm Action name this rule reacts to. Validated against
    #: ``actions.watched_events()`` when written, not by a DB constraint:
    #: what is subscribable is a property of what is installed, and a
    #: constraint would outlive an uninstall.
    event_type = models.CharField(max_length=128, db_index=True)

    #: Delivery type name from the merge-registry (``registry.py``).
    delivery = models.CharField(max_length=32)

    #: Delivery-type-specific destination: ``{"url": ...}``,
    #: ``{"notification_type": ..., "email": ...}``, ``{"stream": ...}``,
    #: ``{"path": ...}``. Validated by ``registry.validate_target``.
    target = models.JSONField(default=dict)

    #: The design doc's ``filter``: a JSON predicate over the event payload
    #: (``filters.py``). Named in full here because ``filter`` on a model is
    #: a word that reads as the queryset method at every call site.
    payload_filter = models.JSONField(default=dict, blank=True)

    #: HMAC secret for signed delivery types. Stored in the clear because
    #: signing needs the key itself (a hash cannot sign) — it is a shared
    #: secret, exactly like a receiver's copy. Never serialized except in
    #: the response to the call that created or rotated it.
    secret = models.CharField(max_length=128, blank=True, default="")

    #: The owner's own label for the rule ("CRM sync"). Never interpreted.
    description = models.CharField(max_length=255, blank=True, default="")
    #: Whether the matcher considers this rule at all.
    is_active = models.BooleanField(default=True, db_index=True)

    #: Consecutive dead-letters. Reset by any success; at
    #: ``DISABLE_AFTER_DEAD`` the rule deactivates itself and says so on the
    #: bus, because a receiver that has been gone for a week should stop
    #: costing the deployment a retry ladder per event.
    consecutive_failures = models.PositiveIntegerField(default=0)
    #: When the strike count deactivated the rule (null if a human did).
    disabled_at = models.DateTimeField(null=True, blank=True)
    #: Last SUCCESSFUL delivery — the "is this receiver alive" column.
    last_delivery_at = models.DateTimeField(null=True, blank=True)

    #: Who authored the rule, for the audit trail. Nulled on account
    #: deletion; ``owner_id`` is what scoping actually reads.
    created_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    #: Authoring time.
    created_at = models.DateTimeField(auto_now_add=True)
    #: Last edit, including the counter updates the machine writes.
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "webhooks_subscription"
        ordering = ("-created_at", "-id")
        indexes = [
            # The dispatcher's only query: active rules for one event type.
            models.Index(fields=["event_type", "is_active"], name="wh_sub_event_active"),
            models.Index(fields=["owner_id", "-created_at"], name="wh_sub_owner_time"),
        ]

    def __str__(self):
        return f"{self.event_type} -> {self.delivery}"


@access.sensitive  # carries a snapshot of the emitting module's payload
class Delivery(models.Model):
    """One (event, subscription) pair and everything that happened to it.

    ``idempotency_key`` is unique and is the whole at-least-once story:
    Action delivery is at-least-once by contract, so the same event reaches
    the dispatcher more than once as a matter of course. The key is
    ``<event_id>:<subscription_id>``, so a redelivered event finds its row
    already there and plans nothing. It is also what the receiver gets in
    ``X-Stapel-Delivery``, which lets IT de-duplicate our retries with the
    same value.
    """

    #: Delivery identity. Travels to the receiver as ``X-Stapel-Delivery``,
    #: which is what lets IT de-duplicate our retries.
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    #: The rule that planned this. Deleting a rule deletes its evidence —
    #: an owner who removes a subscription is asking for exactly that.
    subscription = models.ForeignKey(
        Subscription, on_delete=models.CASCADE, related_name="deliveries"
    )

    #: Denormalized from the subscription so a report needs no join and an
    #: erased subscription still leaves legible evidence.
    event_type = models.CharField(max_length=128, db_index=True)
    #: The emitting ``Event.event_id`` — the correlation handle back to the
    #: outbox row and to every other subscriber's delivery of the same fact.
    event_id = models.CharField(max_length=64, blank=True, default="")
    #: ``<event_id>:<subscription_id>``, unique. See the class docstring:
    #: this is the whole at-least-once story.
    idempotency_key = models.CharField(max_length=200, unique=True)

    #: Snapshot of the payload as it was matched. Snapshotted rather than
    #: re-read at delivery time: a retry an hour later must deliver the fact
    #: that happened, not the state as it is now.
    payload = models.JSONField(default=dict, blank=True)

    #: pending -> retrying -> succeeded | dead. See DELIVERY_STATUSES.
    status = models.CharField(
        max_length=16, choices=DELIVERY_STATUSES, default=STATUS_PENDING, db_index=True
    )
    #: Tries so far. Compared against MAX_ATTEMPTS to decide the dead letter.
    attempts = models.PositiveIntegerField(default=0)
    #: When the drain may pick this up. Also the claim lease: a claimed row
    #: has it pushed into the future.
    next_attempt_at = models.DateTimeField(null=True, blank=True, db_index=True)
    #: When the last try happened.
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    #: When the row reached a terminal state.
    completed_at = models.DateTimeField(null=True, blank=True)

    #: The receiver's HTTP status on the last try, where there was one.
    response_status = models.IntegerField(null=True, blank=True)
    #: Free text, bounded by the transport's response cap. The receiver's
    #: own words are what makes a dead letter actionable.
    last_error = models.TextField(blank=True, default="")

    #: When the fact was matched. This is the ``created_at`` the receiver
    #: sees in the envelope, so it is stable across retries.
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    #: Last write, attempt bookkeeping and claims included.
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "webhooks_delivery"
        ordering = ("-created_at", "-id")
        indexes = [
            # The drain's claim query.
            models.Index(fields=["status", "next_attempt_at"], name="wh_del_status_next"),
            models.Index(fields=["subscription", "-created_at"], name="wh_del_sub_time"),
        ]

    def __str__(self):
        return f"{self.event_type} [{self.status}] x{self.attempts}"

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES
