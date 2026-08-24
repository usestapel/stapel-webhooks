"""i18n error keys of stapel-webhooks.

Only ``error.<status>.webhooks_<slug>`` keys are OWNED by this package —
human-readable strings are translations, never literals in responses. The
English registry below is the source; ``translations/errors.<lang>.json``
ships the localized catalogues in the same release (owning keys means
shipping their catalogues).
"""
from stapel_core.django.api.errors import ErrorKeysView, register_service_errors

# ── Subscription authoring ───────────────────────────────────────────
ERR_400_UNKNOWN_EVENT = "error.400.webhooks_unknown_event"
ERR_400_UNKNOWN_DELIVERY = "error.400.webhooks_unknown_delivery"
ERR_400_INVALID_TARGET = "error.400.webhooks_invalid_target"
ERR_400_INVALID_FILTER = "error.400.webhooks_invalid_filter"
ERR_400_INSECURE_TARGET = "error.400.webhooks_insecure_target"
ERR_400_NOT_SIGNED_TYPE = "error.400.webhooks_not_signed_type"
ERR_403_FORBIDDEN = "error.403.webhooks_forbidden"
ERR_404_SUBSCRIPTION = "error.404.webhooks_subscription_not_found"
ERR_404_DELIVERY = "error.404.webhooks_delivery_not_found"
ERR_409_SUBSCRIPTION_CAP = "error.409.webhooks_subscription_cap"
ERR_409_NOT_REPLAYABLE = "error.409.webhooks_not_replayable"

STAPEL_WEBHOOKS_ERRORS = {
    ERR_400_UNKNOWN_EVENT: "No installed module emits the event {event_type}",
    ERR_400_UNKNOWN_DELIVERY: "Unknown delivery type {delivery}",
    ERR_400_INVALID_TARGET: "The target does not fit this delivery type",
    ERR_400_INVALID_FILTER: "The filter is not a valid payload predicate",
    ERR_400_INSECURE_TARGET: "A webhook target must be an https URL",
    ERR_400_NOT_SIGNED_TYPE: "This delivery type carries no signature, so it has no secret to rotate",
    ERR_403_FORBIDDEN: "You do not have access to this subscription",
    ERR_404_SUBSCRIPTION: "Subscription not found",
    ERR_404_DELIVERY: "Delivery not found",
    ERR_409_SUBSCRIPTION_CAP: "You already have the maximum number of subscriptions",
    ERR_409_NOT_REPLAYABLE: "Only a dead-lettered delivery can be replayed",
}

#: What a client can actually DO about each refusal (core's REMEDIATION_VOCAB).
STAPEL_WEBHOOKS_REMEDIATION = {
    ERR_400_UNKNOWN_EVENT: "fix_input",
    ERR_400_UNKNOWN_DELIVERY: "fix_input",
    ERR_400_INVALID_TARGET: "fix_input",
    ERR_400_INVALID_FILTER: "fix_input",
    ERR_400_INSECURE_TARGET: "fix_input",
    ERR_400_NOT_SIGNED_TYPE: "fix_input",
    ERR_403_FORBIDDEN: "contact_support",
    ERR_404_SUBSCRIPTION: "verify",
    ERR_404_DELIVERY: "verify",
    ERR_409_SUBSCRIPTION_CAP: "contact_support",
    ERR_409_NOT_REPLAYABLE: "verify",
}

register_service_errors(STAPEL_WEBHOOKS_ERRORS, remediation=STAPEL_WEBHOOKS_REMEDIATION)


class WebhooksErrorKeysView(ErrorKeysView):
    """The error-key listing the stapel-translate collector reads.

    Mounted at ``error-keys/`` (the stapel-cdn / workspaces / profiles
    convention). Without it the collector reports this service as having no
    endpoint and its catalogues never get regenerated.
    """

    def get_service_errors(self):
        return STAPEL_WEBHOOKS_ERRORS


__all__ = (
    [name for name in dir() if name.startswith("ERR_")]
    + [
        "STAPEL_WEBHOOKS_ERRORS",
        "STAPEL_WEBHOOKS_REMEDIATION",
        "WebhooksErrorKeysView",
    ]
)
