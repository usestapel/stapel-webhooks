"""The retry / dead-letter state machine — the part an outage exercises.

Every transition is asserted from the outside (status, attempts,
next_attempt_at, the emitted fact), because those are what an operator and
a downstream consumer actually see.
"""
import pytest
from django.test import override_settings
from django.utils import timezone
from stapel_core.comm import on_action

from stapel_webhooks import services
from stapel_webhooks.models import (
    STATUS_DEAD,
    STATUS_PENDING,
    STATUS_RETRYING,
    STATUS_SUCCEEDED,
    Delivery,
    Subscription,
)
from stapel_webhooks.transport import TransportError

pytestmark = pytest.mark.django_db

SETTINGS = {
    "WATCH_EVENTS": ["listing.published"],
    "MAX_ATTEMPTS": 3,
    "BACKOFF_BASE_SECONDS": 10,
    "BACKOFF_FACTOR": 3.0,
    "BACKOFF_CAP_SECONDS": 100,
    "JITTER_RATIO": 0,
    "DISABLE_AFTER_DEAD": 2,
}


@pytest.fixture
def env(settings, transport):
    def _build(responses=None):
        instance = transport(responses)
        settings.STAPEL_WEBHOOKS = {**SETTINGS, "TRANSPORT": instance}
        rule = services.create_subscription(
            event_type="listing.published",
            delivery="webhook",
            target={"url": "https://example.com/hook"},
        )
        delivery, _ = services.plan_delivery(
            rule, event_type="listing.published", event_id="e1", payload={"id": "1"}
        )
        return instance, rule, delivery

    return _build


class TestBackoff:
    @override_settings(STAPEL_WEBHOOKS=SETTINGS)
    def test_ladder_is_exponential_and_capped(self):
        assert services.backoff_seconds(1, jitter=False) == 10
        assert services.backoff_seconds(2, jitter=False) == 30
        assert services.backoff_seconds(3, jitter=False) == 90
        assert services.backoff_seconds(4, jitter=False) == 100  # cap
        assert services.backoff_seconds(50, jitter=False) == 100

    @override_settings(STAPEL_WEBHOOKS={**SETTINGS, "JITTER_RATIO": 0.5})
    def test_jitter_stays_within_its_ratio(self):
        values = {services.backoff_seconds(1) for _ in range(50)}
        assert all(5 <= v <= 15 for v in values)
        assert len(values) > 1  # it actually jitters


class TestTransitions:
    def test_success_is_terminal_and_announced(self, env):
        received = []
        on_action("webhooks.delivery.succeeded", schema=None)(received.append)
        _, rule, delivery = env([200])
        services.attempt(delivery)
        assert delivery.status == STATUS_SUCCEEDED
        assert delivery.attempts == 1
        assert delivery.completed_at is not None
        assert delivery.next_attempt_at is None
        assert received and received[0].payload["delivery_id"] == str(delivery.id)
        rule.refresh_from_db()
        assert rule.consecutive_failures == 0
        assert rule.last_delivery_at is not None

    def test_server_error_schedules_a_retry(self, env):
        _, _, delivery = env([503])
        before = timezone.now()
        services.attempt(delivery)
        assert delivery.status == STATUS_RETRYING
        assert delivery.attempts == 1
        assert delivery.response_status == 503
        assert (delivery.next_attempt_at - before).total_seconds() >= 9

    def test_client_error_dead_letters_on_the_first_attempt(self, env):
        """Eight identical 400s teach nobody anything."""
        _, _, delivery = env([400])
        services.attempt(delivery)
        assert delivery.status == STATUS_DEAD
        assert delivery.attempts == 1

    def test_429_is_retryable_unlike_other_4xx(self, env):
        _, _, delivery = env([429])
        services.attempt(delivery)
        assert delivery.status == STATUS_RETRYING

    def test_the_ladder_ends_in_the_dead_letter(self, env):
        received = []
        on_action("webhooks.delivery.dead", schema=None)(received.append)
        _, _, delivery = env([500])
        for _ in range(3):
            services.attempt(delivery)
        assert delivery.status == STATUS_DEAD
        assert delivery.attempts == 3
        assert delivery.next_attempt_at is None
        assert received and received[0].payload["attempts"] == 3

    def test_network_failure_is_retryable_and_ssrf_refusal_is_not(self, env):
        _, _, delivery = env([TransportError("connection_failed", "boom", retryable=True)])
        services.attempt(delivery)
        assert delivery.status == STATUS_RETRYING

        _, _, second = env([TransportError("blocked_ip", "private", retryable=False)])
        services.attempt(second)
        assert second.status == STATUS_DEAD
        assert "blocked_ip" in second.last_error

    def test_a_raising_handler_is_a_retryable_failure(self, env, settings):
        _, rule, delivery = env([200])
        settings.STAPEL_WEBHOOKS = {
            **settings.STAPEL_WEBHOOKS,
            "DELIVERY_TYPES": {"webhook": {"handler": "stapel_webhooks.tests.test_delivery_machine.exploding_handler"}},
        }
        services.attempt(delivery)
        assert delivery.status == STATUS_RETRYING
        assert "RuntimeError" in delivery.last_error

    def test_a_terminal_row_is_never_delivered_twice(self, env):
        instance, _, delivery = env([200])
        services.attempt(delivery)
        services.attempt(delivery)
        assert len(instance.calls) == 1

    def test_an_unregistered_delivery_type_dead_letters(self, env, settings):
        _, _, delivery = env([200])
        settings.STAPEL_WEBHOOKS = {**settings.STAPEL_WEBHOOKS, "DELIVERY_TYPES": {"webhook": None}}
        services.attempt(delivery)
        assert delivery.status == STATUS_DEAD


def exploding_handler(context):
    raise RuntimeError("handler exploded")


class TestSubscriptionHealth:
    def test_consecutive_dead_letters_disable_the_rule(self, env):
        announced = []
        on_action("webhooks.subscription.disabled", schema=None)(announced.append)
        _, rule, first = env([400])
        services.attempt(first)
        rule.refresh_from_db()
        assert rule.consecutive_failures == 1
        assert rule.is_active is True

        second, _ = services.plan_delivery(
            rule, event_type="listing.published", event_id="e2", payload={}
        )
        services.attempt(second)
        rule.refresh_from_db()
        assert rule.is_active is False
        assert rule.disabled_at is not None
        assert announced and announced[0].payload["subscription_id"] == str(rule.id)

    def test_a_success_resets_the_strike_count(self, env):
        _, rule, first = env([400, 200])
        services.attempt(first)
        rule.refresh_from_db()
        assert rule.consecutive_failures == 1
        second, _ = services.plan_delivery(
            rule, event_type="listing.published", event_id="e2", payload={}
        )
        services.attempt(second)
        rule.refresh_from_db()
        assert rule.consecutive_failures == 0

    def test_reactivating_clears_the_strikes(self, env):
        _, rule, _ = env([400])
        rule.consecutive_failures = 5
        rule.is_active = False
        rule.save()
        services.update_subscription(rule, is_active=True)
        assert rule.consecutive_failures == 0
        assert rule.disabled_at is None


class TestReplay:
    def test_only_a_dead_row_is_replayable(self, env):
        _, _, delivery = env([200])
        services.attempt(delivery)
        with pytest.raises(services.WebhooksError) as exc:
            services.replay(delivery)
        assert exc.value.status == 409

    def test_replay_restores_the_full_ladder(self, env):
        _, _, delivery = env([400])
        services.attempt(delivery)
        services.replay(delivery)
        assert delivery.status == STATUS_PENDING
        assert delivery.attempts == 0
        assert delivery.last_error == ""
        assert delivery.next_attempt_at is not None


class TestDrain:
    def test_drain_attempts_what_is_due(self, env):
        instance, rule, _ = env([200])
        for i in range(3):
            services.plan_delivery(
                rule, event_type="listing.published", event_id=f"x{i}", payload={}
            )
        counts = services.drain()
        assert counts["attempted"] == 4
        assert counts["succeeded"] == 4
        assert len(instance.calls) == 4

    def test_a_row_not_yet_due_is_left_alone(self, env):
        _, _, delivery = env([503])
        services.attempt(delivery)
        assert services.drain()["attempted"] == 0

    def test_claiming_is_exclusive(self, env):
        _, _, delivery = env([200])
        assert services.claim(delivery.pk) is True
        assert services.claim(delivery.pk) is False

    def test_batch_size_bounds_a_pass(self, env, settings):
        _, rule, _ = env([200])
        for i in range(5):
            services.plan_delivery(rule, event_type="listing.published", event_id=f"y{i}", payload={})
        settings.STAPEL_WEBHOOKS = {**settings.STAPEL_WEBHOOKS, "DRAIN_BATCH_SIZE": 2}
        assert services.drain()["attempted"] == 2

    def test_one_exploding_row_does_not_stop_the_drain(self, env, monkeypatch):
        _, rule, _ = env([200])
        services.plan_delivery(rule, event_type="listing.published", event_id="z", payload={})
        calls = {"n": 0}
        real = services.attempt

        def flaky(delivery):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return real(delivery)

        monkeypatch.setattr(services, "attempt", flaky)
        counts = services.drain()
        assert counts["attempted"] == 2


class TestRetention:
    def test_purge_respects_the_two_horizons(self, env):
        _, rule, _ = env([200])
        old = timezone.now() - timezone.timedelta(days=60)
        Delivery.objects.all().update(status=STATUS_SUCCEEDED, completed_at=old)
        dead, _ = services.plan_delivery(rule, event_type="listing.published", event_id="d", payload={})
        Delivery.objects.filter(pk=dead.pk).update(status=STATUS_DEAD, completed_at=old)
        counts = services.purge_deliveries()
        assert counts["succeeded"] == 1
        assert counts["dead"] == 0  # 60 days < the 90-day evidence horizon
        assert Delivery.objects.count() == 1


class TestInlineDispatch:
    def test_inline_mode_delivers_after_commit(
        self, settings, transport, django_capture_on_commit_callbacks
    ):
        instance = transport([200])
        settings.STAPEL_WEBHOOKS = {**SETTINGS, "TRANSPORT": instance, "DISPATCH_MODE": "inline"}
        services.create_subscription(
            event_type="listing.published", delivery="webhook",
            target={"url": "https://example.com/hook"},
        )
        from stapel_core.bus.event import Event

        event = Event(event_type="listing.published", service="tests", payload={"id": "1"})
        # The attempt rides transaction.on_commit: the delivery leaves iff
        # the mutation that announced it committed.
        with django_capture_on_commit_callbacks(execute=True):
            services.dispatch_event(event)
        assert len(instance.calls) == 1
        assert Delivery.objects.get().status == STATUS_SUCCEEDED

    def test_deferred_mode_only_plans(self, env):
        instance, _, _ = env([200])
        from stapel_core.bus.event import Event

        services.dispatch_event(Event(event_type="listing.published", service="tests", payload={}))
        assert instance.calls == []
        assert Subscription.objects.count() == 1
