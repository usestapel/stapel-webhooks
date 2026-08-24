"""Where the reaction layer attaches to the comm bus.

Every other module subscribes to a fixed list of topics it was written
knowing. This one cannot: what it reacts to is whatever the deployment
emits. So the subscription set is *derived* —

    watched = catalog(schemas/emits of every installed app)
            + STAPEL_WEBHOOKS["WATCH_EVENTS"]
            - STAPEL_WEBHOOKS["IGNORE_EVENTS"]

— and one handler is wired per topic at ``AppConfig.ready()``. Install a
module, and its facts become subscribable; uninstall it, and they stop
being offered. Nothing about that reads the database, which is what makes
it legal at ready() time (house law §49).

The same set is the vocabulary ``services.validate_subscription`` accepts,
so "you can subscribe to it" and "we are listening for it" are one fact
rather than two lists that drift.

Transport is chosen by ``STAPEL_COMM`` (in-process in a monolith, bus
consumer in microservices); this code is identical in both, and the handler
is idempotent because Action delivery is at-least-once — the idempotency key
on ``Delivery`` is what absorbs the redelivery.
"""
from __future__ import annotations

import logging

from stapel_core.comm import subscribe_action

logger = logging.getLogger(__name__)

#: Topics this process has already wired. ``subscribe_action`` appends to a
#: handler list, so a second call would double-deliver every event; the set
#: is what makes calling this at every ready() (tests, reloads) a no-op.
_SUBSCRIBED: set[str] = set()

#: Topics named at runtime by ``watch_event()``, on top of the derived set.
_runtime_events: set[str] = set()


def handle_event(event) -> None:
    """Every watched topic lands here. Never raises into the bus.

    A dispatcher exception would fail the whole Action delivery for every
    other subscriber of that topic (in-process transport raises
    ``ActionDeliveryError`` after running them all). The reaction layer is a
    consumer of other modules' facts, not a gate on them.
    """
    from . import services

    try:
        services.dispatch_event(event)
    except Exception:  # noqa: BLE001 — see the docstring
        logger.exception(
            "webhooks: dispatch failed for %s", getattr(event, "event_type", "?")
        )


def watch_event(name: str) -> None:
    """Add one topic at runtime and wire it immediately.

    For the host that emits an event no package ships a schema for and does
    not want to restart to say so. Idempotent.
    """
    _runtime_events.add(str(name))
    subscribe_watched_events()


def reset_runtime_events() -> None:
    """Tests only: drop runtime topics (the wiring itself stays)."""
    _runtime_events.clear()


def watched_events() -> frozenset:
    """The topics this deployment reacts to — and the only ones a
    subscription may name."""
    from .catalog import catalog_event_names
    from .conf import webhooks_settings
    from .services import ignored_events

    names: set[str] = set()
    if webhooks_settings.WATCH_CATALOG:
        names.update(catalog_event_names())
    names.update(str(n) for n in (webhooks_settings.WATCH_EVENTS or ()))
    names.update(_runtime_events)
    return frozenset(names - ignored_events())


def subscribe_watched_events() -> int:
    """Wire one handler per watched topic. Returns how many were added.

    Safe to call repeatedly: already-wired topics are skipped. Topics that
    disappear from the set (a setting changed at runtime) stay wired — the
    action registry has no unsubscribe, and a handler that finds no matching
    subscription costs one indexed query.
    """
    added = 0
    for name in sorted(watched_events()):
        if name in _SUBSCRIBED:
            continue
        subscribe_action(name, handle_event)
        _SUBSCRIBED.add(name)
        added += 1
    return added


def subscribed_events() -> frozenset:
    """Topics wired in THIS process (diagnostics, and the system check)."""
    return frozenset(_SUBSCRIBED)


__all__ = [
    "handle_event",
    "reset_runtime_events",
    "subscribe_watched_events",
    "subscribed_events",
    "watch_event",
    "watched_events",
]
