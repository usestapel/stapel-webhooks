"""comm surface of stapel-webhooks.

Two Functions, and they are the two questions another service asks a
reaction layer:

- ``webhooks.event_catalog`` — "what can be subscribed to here?" This is
  what lets a Studio-side UI (or another service's admin) render the
  subscription builder without importing this module or reading its
  database.
- ``webhooks.dispatch`` — "react to this fact". The path for a service whose
  events do NOT travel on this deployment's bus: a gateway, an inbound
  integration, a module running with ``ACTION_TRANSPORT`` pointed elsewhere.
  In a monolith nobody calls it, because ``actions.py`` already sees
  everything.

Every Function carries a JSON schema in ``schemas/functions/`` — tests run
with ``VALIDATE_SCHEMAS`` on, so a payload drifting from its schema fails
loudly. Registration happens on import from ``apps.py:ready()``.
"""
from stapel_core.comm import function


@function("webhooks.event_catalog")
def event_catalog_function(payload):
    """Input: ``{"refresh": bool?}``; output: ``{"events": [...]}``.

    The full JSON schema of each event is deliberately NOT returned: the
    answer is a picker's vocabulary, and shipping every schema makes a
    response that grows with the fleet. ``properties`` is what a filter
    builder needs.
    """
    from .catalog import event_catalog

    catalog = event_catalog(refresh=bool((payload or {}).get("refresh")))
    return {
        "events": [
            {
                "event": entry["event"],
                "module": entry["module"],
                "package": entry["package"],
                "description": entry["description"],
                "required": entry["required"],
                "properties": entry["properties"],
            }
            for entry in sorted(catalog.values(), key=lambda e: e["event"])
        ]
    }


@function("webhooks.dispatch")
def dispatch_function(payload):
    """Input: ``{"event_type", "payload"?, "event_id"?}``; output:
    ``{"planned": int}``.

    Idempotent through the same key the bus path uses: calling twice with
    the same ``event_id`` plans once.
    """
    from stapel_core.bus.event import Event

    from . import services

    data = payload or {}
    event = Event(
        event_type=data["event_type"],
        service=data.get("service") or "external",
        payload=data.get("payload") or {},
    )
    if data.get("event_id"):
        event.event_id = str(data["event_id"])
    return {"planned": len(services.dispatch_event(event))}


__all__ = ["dispatch_function", "event_catalog_function"]
