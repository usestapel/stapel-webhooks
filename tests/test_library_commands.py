"""The operator surface: management commands and the schedulable callables.

A retention policy nobody schedules is a promise, not a mechanism — and a
drain nobody can run by hand is a queue nobody can unblock during an
incident. Both are asserted here, including the fact that the beat-schedule
factory is the only thing in the module that needs celery.
"""
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from stapel_webhooks import services, tasks
from stapel_webhooks.models import STATUS_SUCCEEDED, Delivery

pytestmark = pytest.mark.django_db


@pytest.fixture
def queued(settings, transport, watch_any_event):
    instance = transport([200])
    settings.STAPEL_WEBHOOKS = {
        **settings.STAPEL_WEBHOOKS, "TRANSPORT": instance,
    }
    rule = services.create_subscription(
        event_type="listing.published", delivery="webhook",
        target={"url": "https://example.com/hook"},
    )
    services.plan_delivery(rule, event_type="listing.published", event_id="e1", payload={})
    return instance, rule


class TestDeliverCommand:
    def test_one_pass_drains_what_is_due(self, queued):
        instance, _ = queued
        out = StringIO()
        call_command("deliver_webhooks", stdout=out)
        assert "attempted=1 succeeded=1" in out.getvalue()
        assert len(instance.calls) == 1

    def test_limit_is_honoured(self, queued):
        _, rule = queued
        services.plan_delivery(rule, event_type="listing.published", event_id="e2", payload={})
        out = StringIO()
        call_command("deliver_webhooks", limit=1, stdout=out)
        assert "attempted=1" in out.getvalue()


class TestPurgeCommand:
    def test_purges_past_the_horizon(self, queued):
        old = timezone.now() - timezone.timedelta(days=30)
        Delivery.objects.all().update(status=STATUS_SUCCEEDED, completed_at=old)
        out = StringIO()
        call_command("purge_webhook_deliveries", stdout=out)
        assert "succeeded=1" in out.getvalue()
        assert Delivery.objects.count() == 0


class TestCatalogCommand:
    def test_human_output_lists_watched_events(self, watch_any_event):
        out = StringIO()
        call_command("webhooks_event_catalog", stdout=out)
        text = out.getvalue()
        assert "listing.published" in text
        assert "subscribable event(s)" in text

    def test_json_output_is_machine_readable(self, watch_any_event):
        import json

        out = StringIO()
        call_command("webhooks_event_catalog", "--json", stdout=out)
        rows = json.loads(out.getvalue())
        assert {row["event"] for row in rows} >= {"listing.published"}

    def test_says_so_when_nothing_is_watched(self, settings):
        settings.STAPEL_WEBHOOKS = {"WATCH_CATALOG": False, "WATCH_EVENTS": []}
        out = StringIO()
        call_command("webhooks_event_catalog", stdout=out)
        assert "no watched events" in out.getvalue()


class TestSchedulableCallables:
    def test_drain_task_returns_counts(self, queued):
        assert tasks.drain_deliveries()["succeeded"] == 1

    def test_purge_task_returns_counts(self, queued):
        assert tasks.purge_deliveries() == {"succeeded": 0, "dead": 0}

    def test_task_names_are_stable(self):
        """A beat schedule references these strings; renaming the function
        without renaming the task silently stops the schedule."""
        assert tasks.DRAIN_TASK_NAME == "stapel_webhooks.tasks.drain_deliveries"
        assert tasks.PURGE_TASK_NAME == "stapel_webhooks.tasks.purge_deliveries"

    def test_beat_schedule_needs_celery_and_nothing_else_does(self):
        """The house pattern: guard the optional dependency, never widen the
        runtime one to make a test pass."""
        pytest.importorskip("celery.schedules")
        schedule = tasks.get_webhooks_beat_schedule()
        assert schedule["webhooks-drain"]["task"] == tasks.DRAIN_TASK_NAME
        assert schedule["webhooks-purge"]["task"] == tasks.PURGE_TASK_NAME
