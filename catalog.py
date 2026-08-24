"""``event_catalog()`` — what a subscription may subscribe to, generated.

The design (docs/pending/forms-events-transports.md §3) asks for "an
auto-summary of every emit of every installed module, from
``schemas/emits/``: what can be subscribed to at all, generated rather than
maintained by hand". This is that.

The rule it enforces is small and load-bearing: **a fact is subscribable
because a package ships its schema, not because somebody added it to a
list.** Install stapel-moderation and ``moderation.report.received`` becomes
selectable in the subscription UI; uninstall it and the event stops being
offered — with no release of this module either way.

Sources, in one pass:

- every installed app's ``<app.path>/schemas/emits/*.json`` (the fleet's
  layout: one file per event, named after it);
- every directory listed in ``STAPEL_WEBHOOKS["EXTRA_CATALOG_PATHS"]``,
  which is how an L1 library with no AppConfig — and therefore no app path —
  contributes its events.

No database, no network, no import of the emitting package: this reads
files, which is why ``apps.ready()`` may call it (house law §49).
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_CACHE: dict[str, dict] | None = None
_LOCK = threading.Lock()

#: Sub-path, relative to a package root, holding one JSON schema per emit.
EMITS_DIR = ("schemas", "emits")


def _entry(path: Path, package: str, module: str) -> dict | None:
    """One catalog row from one schema file, or None if unreadable.

    An unreadable schema is logged and skipped rather than raised: the
    catalog is consulted at boot (subscription wiring) and by the API, and a
    single malformed file in one installed package must not take a
    deployment down. The file's *name* is the event name — the schema's own
    ``title`` is cross-checked and preferred only when it agrees.
    """
    event = path.stem
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("webhooks catalog: unreadable emit schema %s: %s", path, exc)
        return None
    if not isinstance(schema, dict):
        logger.warning("webhooks catalog: emit schema %s is not an object", path)
        return None
    title = schema.get("title")
    if title and title != event:
        logger.warning(
            "webhooks catalog: %s declares title %r — the file name is the "
            "event name, so the schema title is ignored",
            path, title,
        )
    return {
        "event": event,
        "package": package,
        "module": module,
        "description": schema.get("description") or "",
        "required": list(schema.get("required") or ()),
        "properties": sorted((schema.get("properties") or {}).keys()),
        "schema": schema,
        "source": str(path),
    }


def _scan(root: Path, package: str, module: str, into: dict) -> None:
    emits = root.joinpath(*EMITS_DIR)
    if not emits.is_dir():
        return
    for path in sorted(emits.glob("*.json")):
        row = _entry(path, package, module)
        if row is None:
            continue
        # First writer wins, deterministically: apps are scanned in
        # INSTALLED_APPS order, so a duplicate event name resolves the same
        # way on every process of the deployment instead of by dict order.
        into.setdefault(row["event"], row)


def build_catalog() -> dict[str, dict]:
    """Scan every source and return ``{event_name: entry}``. Never cached."""
    catalog: dict[str, dict] = {}
    try:
        from django.apps import apps

        app_configs = list(apps.get_app_configs())
    except Exception:  # pragma: no cover — no app registry (bare import)
        app_configs = []
    for config in app_configs:
        path = getattr(config, "path", None)
        if not path:
            continue
        _scan(Path(path), config.name, config.label, catalog)

    from .conf import webhooks_settings

    for extra in webhooks_settings.EXTRA_CATALOG_PATHS or ():
        root = Path(str(extra))
        _scan(root, root.name, root.name, catalog)
    return catalog


def event_catalog(*, refresh: bool = False) -> dict[str, dict]:
    """Every subscribable event of this deployment, keyed by event name.

    Cached: the answer is a property of what is installed, which cannot
    change inside a process. ``refresh=True`` rebuilds it — tests and the
    management command use it, and so does a host that registers an app
    late.
    """
    global _CACHE
    if _CACHE is not None and not refresh:
        return _CACHE
    with _LOCK:
        if _CACHE is None or refresh:
            _CACHE = build_catalog()
    return _CACHE


def reset_catalog() -> None:
    """Drop the cached catalog (tests, and ``INSTALLED_APPS`` changes)."""
    global _CACHE
    with _LOCK:
        _CACHE = None


def catalog_event_names() -> list[str]:
    """Sorted event names — the subscribable vocabulary."""
    return sorted(event_catalog())


def is_known_event(name: str) -> bool:
    """Whether *name* is declared by some installed package.

    Deliberately not the same question as "may a subscription name it": a
    host emitting an event it has not schema'd lists it in
    ``STAPEL_WEBHOOKS["WATCH_EVENTS"]`` and subscriptions to it are legal.
    See :func:`stapel_webhooks.actions.watched_events`.
    """
    return name in event_catalog()


def event_schema(name: str) -> dict | None:
    """The declared JSON schema of *name*, if the catalog carries one."""
    entry = event_catalog().get(name)
    return entry["schema"] if entry else None


__all__ = [
    "EMITS_DIR",
    "build_catalog",
    "catalog_event_names",
    "event_catalog",
    "event_schema",
    "is_known_event",
    "reset_catalog",
]
