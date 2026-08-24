"""stapel-webhooks — the reaction layer over comm Actions.

One mechanism, four last miles: an emitted fact is matched against the
subscription registry and delivered as a signed HTTP webhook, a
notification, a realtime frame, or an in-process handler. What is
subscribable is generated from installed packages' ``schemas/emits/``
(:func:`event_catalog`), never maintained by hand.

Public API (lazily exported, PEP 562 — importing this package never pulls
in Django or requires configured settings):

- ``webhooks_settings`` — resolved app settings (``stapel_webhooks.conf``);
- ``event_catalog`` — every subscribable event of this deployment;
- ``register_delivery_type`` — add/override/remove a delivery type at
  runtime (the merge-registry seam);
- ``watch_event`` — react to a topic no installed package declares;
- ``sign`` / ``verify`` — the HMAC pair, exported so a receiver inside the
  fleet verifies with the same code that signed.
"""

__all__ = [
    "event_catalog",
    "register_delivery_type",
    "sign",
    "verify",
    "watch_event",
    "webhooks_settings",
]

# name -> submodule that defines it. Resolution is deferred until first
# attribute access so that `import stapel_webhooks` stays Django-free.
_LAZY_EXPORTS = {
    "webhooks_settings": ".conf",
    "event_catalog": ".catalog",
    "register_delivery_type": ".registry",
    "watch_event": ".actions",
    "sign": ".signing",
    "verify": ".signing",
}


def __getattr__(name):
    if name in _LAZY_EXPORTS:
        from importlib import import_module

        value = getattr(import_module(_LAZY_EXPORTS[name], __name__), name)
        globals()[name] = value  # cache for subsequent lookups
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | set(__all__))
