"""Single-module Django settings for stapel-webhooks.

One ``settings.configure(...)`` block serves the test suite and the
migration and management harnesses, which is the point: they cannot drift apart if
there is nothing to drift.
"""
from __future__ import annotations


def settings_kwargs(*, root_urlconf: str = "stapel_webhooks.tests.urls") -> dict:
    """The ``settings.configure(**kwargs)`` for a single-module instance."""
    return dict(
        SECRET_KEY="test-secret-key-not-for-production",
        INSTALLED_APPS=[
            "django.contrib.contenttypes",
            "django.contrib.auth",
            "django.contrib.sessions",
            "django.contrib.admin",
            "django.contrib.messages",
            "stapel_core.django.apps.CommonDjangoConfig",
            "stapel_core.django.users",
            "rest_framework",
            "drf_spectacular",
            "stapel_webhooks",
        ],
        AUTH_USER_MODEL="users.User",
        DATABASES={
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": ":memory:",
            }
        },
        DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
        USE_TZ=True,
        ROOT_URLCONF=root_urlconf,
        CACHES={
            "default": {
                "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            }
        },
        # Synchronous in-process comm with schema validation ON, so the
        # committed contracts in schemas/ are enforced by the tests.
        STAPEL_BUS_BACKEND="stapel_core.bus.backends.memory.MemoryBus",
        STAPEL_COMM={
            "OUTBOX_ENABLED": False,
            "ACTION_TRANSPORT": "inprocess",
            "VALIDATE_SCHEMAS": True,
            # An emit outside a transaction is a bug here, not a warning:
            # the outbox canon is what makes "the row exists" and "the fact
            # was announced" one decision.
            "EMIT_OUTSIDE_ATOMIC": "error",
        },
        MIGRATION_MODULES={
            "users": None,
        },
    )
