"""Admin for stapel-webhooks — an operator peephole, nothing more.

Read-only across the board, and the secret is never displayed. The people
who own reaction rules reach them through the REST surface; what this
registration is for is the operator holding an incident ticket that says
"the CRM stopped getting listings on Tuesday".

Both models are ``@access.sensitive`` (models.py), so the staff mandate
gates even this view at MID clearance — two independent doors, both shut by
default.
"""
from django.contrib import admin

from .models import Delivery, Subscription


class _ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Subscription)
class SubscriptionAdmin(_ReadOnlyAdmin):
    list_display = (
        "id", "event_type", "delivery", "is_active", "consecutive_failures",
        "last_delivery_at", "created_at",
    )
    list_filter = ("delivery", "is_active", "event_type")
    search_fields = ("id", "event_type", "owner_id", "workspace_id", "description")
    #: `secret` is deliberately absent — an operator diagnosing a delivery
    #: never needs the key that would let them forge one.
    fields = (
        "id", "event_type", "delivery", "target", "payload_filter", "description",
        "is_active", "consecutive_failures", "disabled_at", "last_delivery_at",
        "owner_id", "workspace_id", "created_at", "updated_at",
    )
    readonly_fields = fields


@admin.register(Delivery)
class DeliveryAdmin(_ReadOnlyAdmin):
    list_display = (
        "id", "event_type", "status", "attempts", "response_status",
        "next_attempt_at", "created_at",
    )
    list_filter = ("status", "event_type")
    search_fields = ("id", "event_id", "idempotency_key", "subscription__id")
