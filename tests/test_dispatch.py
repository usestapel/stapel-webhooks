"""Matching: one emitted fact in, N delivery rows out."""
import pytest
from django.db import transaction
from django.test import override_settings
from stapel_core.bus.event import Event
from stapel_core.comm import emit

from stapel_webhooks import services
from stapel_webhooks.models import STATUS_DEAD, STATUS_PENDING, Delivery, Subscription

pytestmark = pytest.mark.django_db


def _event(event_type="listing.published", payload=None, event_id="evt-1"):
    event = Event(event_type=event_type, service="tests", payload=payload or {})
    event.event_id = event_id
    return event


@pytest.fixture
def rule(watch_any_event, transport):
    transport()
    return services.create_subscription(
        event_type="listing.published",
        delivery="webhook",
        target={"url": "https://example.com/hook"},
    )


class TestMatching:
    def test_a_matching_event_plans_one_delivery(self, rule):
        planned = services.dispatch_event(_event(payload={"id": "1"}))
        assert len(planned) == 1
        assert planned[0].status == STATUS_PENDING
        assert planned[0].payload == {"id": "1"}
        assert planned[0].event_type == "listing.published"

    def test_other_event_types_are_ignored(self, rule):
        assert services.dispatch_event(_event(event_type="user.registered")) == []

    def test_inactive_rules_do_not_fire(self, rule):
        rule.is_active = False
        rule.save()
        assert services.dispatch_event(_event()) == []

    def test_filter_selects(self, watch_any_event, transport):
        transport()
        services.create_subscription(
            event_type="listing.published",
            delivery="webhook",
            target={"url": "https://example.com/berlin"},
            payload_filter={"city": "berlin"},
        )
        assert services.dispatch_event(_event(payload={"city": "paris"}, event_id="a")) == []
        assert len(services.dispatch_event(_event(payload={"city": "berlin"}, event_id="b"))) == 1

    def test_every_matching_rule_gets_its_own_row(self, watch_any_event, transport):
        transport()
        for i in range(3):
            services.create_subscription(
                event_type="listing.published",
                delivery="webhook",
                target={"url": f"https://example.com/{i}"},
            )
        assert len(services.dispatch_event(_event())) == 3

    def test_a_raising_filter_does_not_silence_the_others(self, watch_any_event, transport, settings):
        transport()
        good = services.create_subscription(
            event_type="listing.published", delivery="webhook",
            target={"url": "https://example.com/ok"},
        )
        services.create_subscription(
            event_type="listing.published", delivery="webhook",
            target={"url": "https://example.com/bad"},
        )

        def explode(payload, predicate):
            if predicate.get("boom"):
                raise RuntimeError("bad predicate")
            return True

        Subscription.objects.exclude(pk=good.pk).update(payload_filter={"boom": True})
        settings.STAPEL_WEBHOOKS = {**settings.STAPEL_WEBHOOKS, "MATCHER": explode}
        planned = services.dispatch_event(_event())
        assert len(planned) == 1
        assert planned[0].subscription_id == good.pk


class TestIdempotency:
    def test_the_same_event_plans_once(self, rule):
        assert len(services.dispatch_event(_event(event_id="same"))) == 1
        assert services.dispatch_event(_event(event_id="same")) == []
        assert Delivery.objects.count() == 1

    def test_the_key_pairs_event_and_subscription(self, rule):
        services.dispatch_event(_event(event_id="e1"))
        row = Delivery.objects.get()
        assert row.idempotency_key == f"e1:{rule.id}"


class TestGuards:
    def test_own_events_are_never_dispatched(self, watch_any_event, transport, settings):
        """Even if a rule exists for one — the loop guard is at dispatch,
        not only in the watch set."""
        transport()
        settings.STAPEL_WEBHOOKS = {
            **settings.STAPEL_WEBHOOKS,
            "WATCH_EVENTS": ["webhooks.delivery.dead"],
            "IGNORE_EVENTS": [],
        }
        services.create_subscription(
            event_type="webhooks.delivery.dead", delivery="webhook",
            target={"url": "https://example.com/hook"},
        )
        settings.STAPEL_WEBHOOKS = {
            **settings.STAPEL_WEBHOOKS,
            "IGNORE_EVENTS": ["webhooks.delivery.dead"],
        }
        assert services.dispatch_event(_event(event_type="webhooks.delivery.dead")) == []

    @override_settings(STAPEL_WEBHOOKS={"MAX_PAYLOAD_BYTES": 32, "WATCH_EVENTS": ["listing.published"]})
    def test_oversized_payload_is_dead_lettered_at_planning_time(self, transport):
        transport()
        services.create_subscription(
            event_type="listing.published", delivery="webhook",
            target={"url": "https://example.com/hook"},
        )
        planned = services.dispatch_event(_event(payload={"blob": "x" * 500}))
        assert planned[0].status == STATUS_DEAD
        assert "MAX_PAYLOAD_BYTES" in planned[0].last_error


class TestBusWiring:
    def test_an_emitted_action_reaches_the_dispatcher(self, watch_any_event, transport):
        """The end-to-end seam: emit() -> comm -> actions.handle_event ->
        a planned delivery. Wired through the same subscribe_watched_events
        the AppConfig calls."""
        from stapel_webhooks import actions

        transport()
        actions.subscribe_watched_events()
        services.create_subscription(
            event_type="test.event", delivery="webhook",
            target={"url": "https://example.com/hook"},
        )
        with transaction.atomic():
            emit("test.event", {"hello": "world"})
        row = Delivery.objects.get()
        assert row.event_type == "test.event"
        assert row.payload == {"hello": "world"}

    def test_the_dispatcher_never_raises_into_the_bus(self, watch_any_event, monkeypatch):
        """A reaction-layer failure must not fail the Action for every other
        subscriber of that topic."""
        from stapel_webhooks import actions

        def boom(event):
            raise RuntimeError("dispatcher down")

        monkeypatch.setattr(services, "dispatch_event", boom)
        actions.handle_event(_event())
