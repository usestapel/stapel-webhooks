"""System checks — every one of them describes a deployment that boots
cleanly and delivers nothing."""
import pytest
from django.test import override_settings

from stapel_webhooks import checks

pytestmark = pytest.mark.django_db


def _ids(results):
    return {result.id for result in results}


class TestWatchSet:
    @override_settings(STAPEL_WEBHOOKS={"WATCH_CATALOG": False, "WATCH_EVENTS": []})
    def test_w001_when_listening_to_nothing(self):
        assert _ids(checks.check_watched_events(None)) == {"webhooks.W001"}

    @override_settings(STAPEL_WEBHOOKS={"WATCH_EVENTS": ["listing.published"]})
    def test_silent_when_something_is_watched(self):
        assert checks.check_watched_events(None) == []


class TestDrainScheduling:
    def test_silent_without_a_beat_schedule(self):
        """A host on cron has no CELERY_BEAT_SCHEDULE; warning it is noise."""
        assert checks.check_drain_scheduled(None) == []

    @override_settings(CELERY_BEAT_SCHEDULE={"something-else": {"task": "other.task"}})
    def test_w002_when_a_schedule_exists_without_the_drain(self):
        assert _ids(checks.check_drain_scheduled(None)) == {"webhooks.W002"}

    @override_settings(
        CELERY_BEAT_SCHEDULE={"d": {"task": "stapel_webhooks.tasks.drain_deliveries"}}
    )
    def test_silent_when_the_drain_is_scheduled(self):
        assert checks.check_drain_scheduled(None) == []

    @override_settings(
        STAPEL_WEBHOOKS={"DISPATCH_MODE": "inline"},
        CELERY_BEAT_SCHEDULE={"something-else": {"task": "other.task"}},
    )
    def test_silent_in_inline_mode(self):
        assert checks.check_drain_scheduled(None) == []


class TestSecurityConfessions:
    def test_silent_by_default(self):
        assert checks.check_insecure_targets(None) == []
        assert checks.check_inline_dispatch(None) == []

    @override_settings(STAPEL_WEBHOOKS={"ALLOW_INSECURE_TARGETS": True})
    def test_w003_when_the_ssrf_guard_is_off(self):
        assert _ids(checks.check_insecure_targets(None)) == {"webhooks.W003"}

    @override_settings(STAPEL_WEBHOOKS={"DISPATCH_MODE": "inline"})
    def test_w004_when_delivery_is_on_the_request_thread(self):
        assert _ids(checks.check_inline_dispatch(None)) == {"webhooks.W004"}


class TestLiveRules:
    def test_silent_with_no_rules(self):
        assert checks.check_live_subscriptions(None) == []

    def test_w005_for_a_rule_whose_event_left_the_catalog(self, watch_any_event, transport):
        from stapel_webhooks import services
        from stapel_webhooks.models import Subscription

        transport()
        services.create_subscription(
            event_type="listing.published", delivery="webhook",
            target={"url": "https://example.com/hook"},
        )
        with override_settings(STAPEL_WEBHOOKS={"WATCH_CATALOG": False, "WATCH_EVENTS": []}):
            assert "webhooks.W005" in _ids(checks.check_live_subscriptions(None))
        assert Subscription.objects.count() == 1

    def test_w006_for_a_rule_whose_delivery_type_was_removed(self, watch_any_event, transport):
        from stapel_webhooks import services

        transport()
        services.create_subscription(
            event_type="listing.published", delivery="webhook",
            target={"url": "https://example.com/hook"},
        )
        with override_settings(STAPEL_WEBHOOKS={
            "WATCH_EVENTS": ["listing.published"],
            "DELIVERY_TYPES": {"webhook": None},
        }):
            assert "webhooks.W006" in _ids(checks.check_live_subscriptions(None))


class TestBootDiscipline:
    def test_this_module_raises_no_error_level_check(self):
        """The ready() discipline in one assertion: every check runs, none
        of them explodes, and this module contributes nothing at E level —
        an E from a library is a deployment that cannot boot."""
        from django.core.checks import Error, run_checks

        ours = [m for m in run_checks() if str(m.id or "").startswith("webhooks.")]
        assert [m for m in ours if isinstance(m, Error)] == []

    def test_checks_survive_an_unmigrated_database(self, monkeypatch):
        """System checks run before migrations on a fresh install; a check
        that queries must swallow the database error rather than replace a
        useful warning with a broken boot."""
        from django.db import OperationalError

        from stapel_webhooks.models import Subscription

        class Exploding:
            def filter(self, *args, **kwargs):
                raise OperationalError("no such table: webhooks_subscription")

        monkeypatch.setattr(Subscription, "objects", Exploding())
        assert checks.check_live_subscriptions(None) == []
