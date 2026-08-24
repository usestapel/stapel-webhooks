"""Django system checks for stapel-webhooks configuration.

Policy (docs/library-standard.md §3.7): E-level for configuration the
service cannot run with; W-level for entries that degrade lazily.

Every check here describes a configuration that LOOKS like a working one.
A reaction layer listening to nothing, a queue nobody drains, an SSRF guard
somebody switched off months ago in a dev branch, a subscription pointed at
a delivery type that is no longer registered — all four boot cleanly, serve
200s, and deliver nothing (or, in one case, deliver somewhere they should
not). That is precisely the class of defect a check exists for.

No check reads the database at import; the two that query do it lazily,
inside the check function, and swallow database errors — a check that
explodes on a fresh install replaces a useful warning with a broken boot.
"""
from django.core import checks


@checks.register(checks.Tags.compatibility)
def check_watched_events(app_configs, **kwargs):
    """W001 — the reaction layer is listening to nothing.

    Happens the moment ``WATCH_CATALOG`` is off and ``WATCH_EVENTS`` was
    never filled, and it is invisible: subscriptions can still be created,
    the API answers, and no delivery is ever planned.
    """
    from .actions import watched_events

    if watched_events():
        return []
    return [checks.Warning(
        "stapel-webhooks watches no events — no subscription can ever fire.",
        hint="Leave STAPEL_WEBHOOKS['WATCH_CATALOG'] on so installed modules' "
             "schemas/emits define the vocabulary, or list topics explicitly in "
             "STAPEL_WEBHOOKS['WATCH_EVENTS'].",
        id="webhooks.W001",
    )]


@checks.register(checks.Tags.compatibility)
def check_drain_scheduled(app_configs, **kwargs):
    """W002 — deferred dispatch with nothing scheduled to drain it.

    Only raised when a beat schedule exists at all: a host draining from
    cron or a systemd timer has no CELERY_BEAT_SCHEDULE to inspect, and
    warning it would be noise.
    """
    from django.conf import settings

    from .conf import webhooks_settings
    from .tasks import DRAIN_TASK_NAME

    if str(webhooks_settings.DISPATCH_MODE or "deferred") == "inline":
        return []
    schedule = getattr(settings, "CELERY_BEAT_SCHEDULE", None)
    if not schedule:
        return []
    for entry in schedule.values():
        if isinstance(entry, dict) and entry.get("task") == DRAIN_TASK_NAME:
            return []
    return [checks.Warning(
        f"CELERY_BEAT_SCHEDULE has no entry for {DRAIN_TASK_NAME} — with "
        "DISPATCH_MODE='deferred' nothing will ever deliver a planned reaction.",
        hint="CELERY_BEAT_SCHEDULE = {**get_webhooks_beat_schedule(), ...} "
             "(stapel_webhooks.tasks), or run the deliver_webhooks management "
             "command from cron.",
        id="webhooks.W002",
    )]


@checks.register(checks.Tags.security)
def check_insecure_targets(app_configs, **kwargs):
    """W003 — the SSRF guard is off.

    ``ALLOW_INSECURE_TARGETS`` permits plaintext http and targets resolving
    to private, loopback and link-local addresses — including the cloud
    metadata endpoint. It is a legitimate dev-box setting and a serious
    production one, so it says so on every boot rather than sitting silent
    in a settings file somebody copied.
    """
    from .conf import webhooks_settings

    if not webhooks_settings.ALLOW_INSECURE_TARGETS:
        return []
    return [checks.Warning(
        "STAPEL_WEBHOOKS['ALLOW_INSECURE_TARGETS'] is on: webhook targets may "
        "be plaintext http and may resolve to private, loopback or "
        "link-local addresses (including the cloud metadata endpoint).",
        hint="Leave it off outside development; use "
             "STAPEL_WEBHOOKS['ALLOWED_TARGET_HOSTS'] to reach a known "
             "in-cluster receiver instead.",
        id="webhooks.W003",
    )]


@checks.register(checks.Tags.compatibility)
def check_inline_dispatch(app_configs, **kwargs):
    """W004 — inline dispatch puts a stranger's endpoint on the request path."""
    from .conf import webhooks_settings

    if str(webhooks_settings.DISPATCH_MODE or "deferred") != "inline":
        return []
    return [checks.Warning(
        "STAPEL_WEBHOOKS['DISPATCH_MODE'] = 'inline': every matching event is "
        "delivered in the emitting process, so a slow receiver becomes this "
        "service's latency.",
        hint="Use the default 'deferred' plus the drain task in production.",
        id="webhooks.W004",
    )]


@checks.register(checks.Tags.compatibility)
def check_live_subscriptions(app_configs, **kwargs):
    """W005 — active subscriptions naming something this process cannot serve.

    The two ways a live rule goes quiet without anybody touching it: the
    module that emitted its event was uninstalled (so the topic left the
    catalog), or its delivery type was removed from the registry. Both leave
    a row that looks perfectly healthy in the admin.
    """
    from .actions import watched_events
    from .registry import get_delivery_types

    rows = _active_rules()
    if not rows:
        return []
    known_events = watched_events()
    known_types = set(get_delivery_types())
    problems = []
    orphan_events = sorted({e for e, _ in rows if e not in known_events})
    orphan_types = sorted({d for _, d in rows if d not in known_types})
    if orphan_events:
        problems.append(checks.Warning(
            "Active subscriptions name events this deployment does not watch: "
            f"{orphan_events}. They will never fire.",
            hint="Install the module that emits them, add them to "
                 "STAPEL_WEBHOOKS['WATCH_EVENTS'], or deactivate the rules.",
            id="webhooks.W005",
        ))
    if orphan_types:
        problems.append(checks.Warning(
            "Active subscriptions name delivery types that are not registered: "
            f"{orphan_types}. Their deliveries dead-letter on the first attempt.",
            hint="Restore the type in STAPEL_WEBHOOKS['DELIVERY_TYPES'] or "
                 "deactivate the rules.",
            id="webhooks.W006",
        ))
    return problems


def _active_rules():
    """``[(event_type, delivery)]`` for live rules; empty on any DB error.

    Swallows every database error on purpose: system checks run before
    migrations on a fresh install.
    """
    from django.db import Error as DatabaseError

    from .models import Subscription

    try:
        return list(
            Subscription.objects.filter(is_active=True)
            .values_list("event_type", "delivery")
            .distinct()[:200]
        )
    except (DatabaseError, Exception):  # noqa: B014 — includes ImproperlyConfigured
        return []


__all__ = [
    "check_drain_scheduled",
    "check_inline_dispatch",
    "check_insecure_targets",
    "check_live_subscriptions",
    "check_watched_events",
]
