from django.apps import AppConfig


class WebhooksConfig(AppConfig):
    name = "stapel_webhooks"
    label = "webhooks"
    verbose_name = "Webhooks: event subscriptions, signed delivery, retries and dead letters"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        # Import-time side effects, one module each.
        from . import checks  # noqa: F401
        from . import errors  # noqa: F401
        from . import functions  # noqa: F401

        # Dynamic Action subscriptions: one per topic this deployment
        # watches (installed modules' schemas/emits + WATCH_EVENTS). The set
        # is derived from files and settings only — no database is touched
        # here, which is what makes it legal at ready() time (house law §49).
        # Idempotent: re-entry (tests, autoreload) adds nothing.
        from . import actions

        actions.subscribe_watched_events()
