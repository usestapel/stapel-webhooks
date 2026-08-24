"""The delivery-type merge-registry — the seam that makes this a reaction
layer rather than a webhook sender.

Semantics are the fleet's (stapel-notifications ``TYPES``, stapel-moderation
``TARGET_TYPES``): built-ins <- ``STAPEL_WEBHOOKS["DELIVERY_TYPES"]`` <-
runtime ``register_delivery_type()``, last layer wins, a spec of ``None``
REMOVES a type. A spec is a plain dict, never an ABC: an interface with one
implementation per host is a class hierarchy pretending to be configuration.

Unlike moderation's target types the built-ins are NOT empty. "Deliver this
fact over HTTP / as a notification / to a live socket / to a dotted path in
the app layer" is the four-way split the design (docs/pending/
forms-events-transports.md §3) names, and all four are universal. What is
host-specific is the *target*, not the delivery kind.

Removing a type is a real operation, not a footnote. ``custom`` executes a
dotted path a subscription row names; a deployment that lets end users
create subscriptions closes it with::

    STAPEL_WEBHOOKS = {"DELIVERY_TYPES": {"custom": None}}

after which the type is unknown to validation, to the API and to the
dispatcher — one place, no fork.
"""
from __future__ import annotations

from typing import Callable, Optional

#: The four deliveries of the reaction layer.
#:
#: ``handler``  — dotted path of ``callable(DeliveryContext) -> DeliveryResult``.
#: ``required_target_keys`` — keys a subscription's ``target`` must carry.
#: ``any_of_target_keys``   — at least one of these must be present (or empty).
#: ``signed``   — whether the delivery carries an HMAC signature, i.e.
#:                whether a subscription of this type needs a secret.
#: ``external`` — whether the delivery leaves the deployment. External
#:                deliveries are the ones the SSRF guard and the payload cap
#:                apply to, and the ones whose targets are user-supplied.
BUILTIN_DELIVERY_TYPES: dict[str, Optional[dict]] = {
    "webhook": {
        "handler": "stapel_webhooks.deliveries.deliver_webhook",
        "required_target_keys": ("url",),
        "any_of_target_keys": (),
        "signed": True,
        "external": True,
        "description": "HTTP POST to a caller-supplied URL, HMAC-signed.",
    },
    "notification": {
        "handler": "stapel_webhooks.deliveries.deliver_notification",
        "required_target_keys": ("notification_type",),
        # request_notification refuses a request that addresses nobody; the
        # registry refuses it one layer earlier, at subscription time, where
        # the person who made the mistake is still on the phone.
        "any_of_target_keys": ("user_id", "email", "phone", "telegram_chat_id"),
        "signed": False,
        "external": False,
        "description": "Email/push/SMS through the notifications module.",
    },
    "ws": {
        "handler": "stapel_webhooks.deliveries.deliver_ws",
        "required_target_keys": ("stream",),
        "any_of_target_keys": (),
        "signed": False,
        "external": False,
        "description": "Ephemeral frame on a realtime stream (comm Signal).",
    },
    "custom": {
        "handler": "stapel_webhooks.deliveries.deliver_custom",
        "required_target_keys": ("path",),
        "any_of_target_keys": (),
        "target_validator": "stapel_webhooks.deliveries.validate_custom_target",
        "signed": False,
        "external": False,
        "description": "In-process dotted-path handler in the app layer.",
    },
}

#: Runtime overrides. Kept separate from the settings layer so tests reset
#: without touching Django settings.
_runtime_types: dict[str, Optional[dict]] = {}


class UnknownDeliveryType(Exception):
    """Raised when a delivery type is not in the effective registry."""


class InvalidTarget(Exception):
    """Raised when a subscription target does not fit its delivery type."""


def register_delivery_type(name: str, spec: Optional[dict]) -> None:
    """Register/override a delivery type at runtime.

    ``spec=None`` removes a type a lower layer (built-ins / settings)
    provided — including a built-in one.
    """
    _runtime_types[name] = spec


def reset_delivery_types() -> None:
    """Tests only: drop runtime delivery-type overrides."""
    _runtime_types.clear()


def get_delivery_types() -> dict[str, dict]:
    """Effective registry: built-ins <- settings <- runtime, ``None``
    removing a key. Only live (non-None) entries are returned."""
    from .conf import webhooks_settings

    merged: dict[str, Optional[dict]] = dict(BUILTIN_DELIVERY_TYPES)
    for source in (webhooks_settings.DELIVERY_TYPES or {}, _runtime_types):
        for name, spec in source.items():
            merged[name] = spec
    return {name: spec for name, spec in merged.items() if spec is not None}


def resolve_delivery(name: str) -> dict:
    """The live spec for *name*, or raise :class:`UnknownDeliveryType`."""
    try:
        return get_delivery_types()[name]
    except KeyError:
        raise UnknownDeliveryType(name) from None


def delivery_handler(name: str) -> Callable:
    """Import and return the handler callable of delivery type *name*.

    Resolved at call time, not at registration: a spec is data, and a host
    that swaps a handler by settings must not need this module reloaded.
    """
    from django.utils.module_loading import import_string

    spec = resolve_delivery(name)
    handler = spec.get("handler")
    if callable(handler):
        return handler
    if not handler:
        raise UnknownDeliveryType(f"delivery type {name!r} declares no handler")
    return import_string(handler)


def validate_target(name: str, target: dict) -> None:
    """Refuse a target that the delivery type cannot use.

    This runs at subscription time on purpose. The alternative — finding out
    at delivery time — turns an authoring mistake into a dead-lettered
    event, discovered by whoever was waiting for the reaction.
    """
    spec = resolve_delivery(name)
    if not isinstance(target, dict):
        raise InvalidTarget("target must be an object")
    missing = [key for key in spec.get("required_target_keys") or () if not target.get(key)]
    if missing:
        raise InvalidTarget(
            f"delivery type {name!r} requires target key(s): {', '.join(sorted(missing))}"
        )
    any_of = spec.get("any_of_target_keys") or ()
    if any_of and not any(target.get(key) for key in any_of):
        raise InvalidTarget(
            f"delivery type {name!r} requires at least one of: {', '.join(sorted(any_of))}"
        )
    # A type may carry its own validator for what a key-presence check
    # cannot express — ``custom`` uses it to enforce the dotted-path
    # allowlist, which is the difference between a seam and an RCE.
    validator = spec.get("target_validator")
    if validator:
        from django.utils.module_loading import import_string

        if not callable(validator):
            validator = import_string(validator)
        validator(target)


def is_signed(name: str) -> bool:
    """Whether subscriptions of this delivery type carry a signing secret."""
    return bool(resolve_delivery(name).get("signed"))


def is_external(name: str) -> bool:
    """Whether this delivery leaves the deployment (SSRF guard applies)."""
    return bool(resolve_delivery(name).get("external"))


__all__ = [
    "BUILTIN_DELIVERY_TYPES",
    "InvalidTarget",
    "UnknownDeliveryType",
    "delivery_handler",
    "get_delivery_types",
    "is_external",
    "is_signed",
    "register_delivery_type",
    "reset_delivery_types",
    "resolve_delivery",
    "validate_target",
]
