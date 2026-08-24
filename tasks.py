"""Scheduled work of stapel-webhooks — the drain that makes the queue move.

Two callables, and the first one is not optional in the way a purge is: with
``DISPATCH_MODE = "deferred"`` (the default) **nothing is delivered until
:func:`drain_deliveries` runs**. A reaction layer whose drain nobody
schedules is a table that fills up, which is why ``checks.py`` warns about
exactly that.

Celery is OPTIONAL. Both functions are plain callables a cron, a systemd
timer or any scheduler can invoke; when celery is installed they are
additionally registered as shared tasks under the stable names below.

Wire them into a host's beat schedule::

    from stapel_webhooks.tasks import get_webhooks_beat_schedule

    CELERY_BEAT_SCHEDULE = {
        **get_webhooks_beat_schedule(),
        ...
    }
"""
import logging

logger = logging.getLogger(__name__)

#: Names a beat schedule must reference (stable across refactors).
DRAIN_TASK_NAME = "stapel_webhooks.tasks.drain_deliveries"
PURGE_TASK_NAME = "stapel_webhooks.tasks.purge_deliveries"


def drain_deliveries() -> dict:
    """Attempt every due delivery. Returns and logs the counts."""
    from .services import drain

    counts = drain()
    if counts["attempted"]:
        logger.info(
            "webhooks drain: %s attempted (%s ok, %s retrying, %s dead, %s skipped)",
            counts["attempted"], counts["succeeded"], counts["retrying"],
            counts["dead"], counts["skipped"],
        )
    return counts


def purge_deliveries() -> dict:
    """Drop delivery rows past their retention horizon."""
    from . import services

    counts = services.purge_deliveries()
    logger.info(
        "webhooks retention purge: %s succeeded, %s dead",
        counts["succeeded"], counts["dead"],
    )
    return counts


def get_webhooks_beat_schedule() -> dict:
    """Beat entries for the drain and the purge, on the configured cadence."""
    from celery.schedules import crontab

    from .conf import webhooks_settings

    return {
        "webhooks-drain": {
            "task": DRAIN_TASK_NAME,
            "schedule": crontab(**dict(webhooks_settings.DRAIN_SCHEDULE or {})),
        },
        "webhooks-purge": {
            "task": PURGE_TASK_NAME,
            "schedule": crontab(**dict(webhooks_settings.PURGE_SCHEDULE or {})),
        },
    }


try:  # pragma: no cover — exercised by whichever profile the host installs
    from celery import shared_task
except ImportError:
    pass
else:
    drain_deliveries = shared_task(name=DRAIN_TASK_NAME)(drain_deliveries)
    purge_deliveries = shared_task(name=PURGE_TASK_NAME)(purge_deliveries)


__all__ = [
    "DRAIN_TASK_NAME",
    "PURGE_TASK_NAME",
    "drain_deliveries",
    "get_webhooks_beat_schedule",
    "purge_deliveries",
]
