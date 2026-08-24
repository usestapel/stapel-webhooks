"""The four last miles — what each delivery type actually sends."""
import json
from datetime import datetime, timezone

import pytest
from django.test import override_settings

from stapel_webhooks import services
from stapel_webhooks.deliveries import (
    DeliveryContext,
    DeliveryResult,
    deliver_custom,
    deliver_notification,
    deliver_webhook,
    deliver_ws,
    encode_body,
    envelope,
)
from stapel_webhooks.signing import verify

pytestmark = pytest.mark.django_db


def _context(**overrides):
    base = dict(
        delivery_id="d-1",
        subscription_id="s-1",
        event_type="listing.published",
        event_id="e-1",
        payload={"id": "1", "city": "berlin"},
        target={"url": "https://example.com/hook"},
        secret="whsec_abc",
        attempt=1,
        created_at=datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return DeliveryContext(**base)


class TestEnvelope:
    def test_shape_is_stable_and_leaks_no_internals(self):
        body = envelope(_context())
        assert set(body) == {"id", "type", "event_id", "created_at", "subscription_id", "data"}
        assert body["data"] == {"id": "1", "city": "berlin"}
        assert "secret" not in json.dumps(body)
        assert "attempt" not in body

    def test_bytes_are_deterministic(self):
        """A retry re-encodes the same row. The signature covers bytes, so
        two encodings of one delivery must be byte-identical or a receiver
        that stores the first signature sees a mismatch on the second."""
        context = _context()
        assert encode_body(context) == encode_body(_context(attempt=7))


class TestWebhookDelivery:
    def test_signed_headers_and_verifiable_body(self, transport):
        instance = transport([200])
        with override_settings(STAPEL_WEBHOOKS={"TRANSPORT": instance}):
            result = deliver_webhook(_context())
        assert result.ok is True
        call = instance.calls[0]
        assert call["url"] == "https://example.com/hook"
        headers = call["headers"]
        assert headers["Content-Type"] == "application/json"
        assert headers["X-Stapel-Event"] == "listing.published"
        assert headers["X-Stapel-Delivery"] == "d-1"
        assert headers["X-Stapel-Attempt"] == "1"
        # The receiver's half of the contract: verify the bytes we sent.
        assert verify("whsec_abc", call["body"], headers["X-Stapel-Signature"]) is True

    def test_no_secret_means_no_signature_header(self, transport):
        instance = transport([200])
        with override_settings(STAPEL_WEBHOOKS={"TRANSPORT": instance}):
            deliver_webhook(_context(secret=""))
        assert "X-Stapel-Signature" not in instance.calls[0]["headers"]

    @override_settings(STAPEL_WEBHOOKS={"SIGNATURE_HEADER": "X-Acme-Sig"})
    def test_header_name_is_configurable(self, transport):
        instance = transport([200])
        with override_settings(STAPEL_WEBHOOKS={"SIGNATURE_HEADER": "X-Acme-Sig", "TRANSPORT": instance}):
            deliver_webhook(_context())
        assert "X-Acme-Sig" in instance.calls[0]["headers"]

    def test_failure_carries_the_receivers_own_words(self, transport):
        from stapel_webhooks.transport import TransportResponse

        class Rude:
            def post(self, url, body, headers=None, **kwargs):
                return TransportResponse(status=422, body="field 'city' unknown")

            def classify(self, status):
                return (False, False)

        with override_settings(STAPEL_WEBHOOKS={"TRANSPORT": Rude()}):
            result = deliver_webhook(_context())
        assert result.ok is False
        assert result.retryable is False
        assert "city" in result.detail


class TestNotificationDelivery:
    def test_requests_a_notification_with_the_payload_as_variables(self, monkeypatch):
        seen = {}

        def fake(notification_type, **kwargs):
            seen.update({"type": notification_type, **kwargs})
            return True

        monkeypatch.setattr(
            "stapel_core.notifications.publish.request_notification", fake
        )
        result = deliver_notification(
            _context(target={"notification_type": "listing_published", "email": "a@b.c"})
        )
        assert result.ok is True
        assert seen["type"] == "listing_published"
        assert seen["email"] == "a@b.c"
        assert seen["variables"]["city"] == "berlin"
        assert seen["variables"]["event_type"] == "listing.published"

    def test_a_refused_request_is_retryable(self, monkeypatch):
        monkeypatch.setattr(
            "stapel_core.notifications.publish.request_notification", lambda *a, **k: False
        )
        result = deliver_notification(_context(target={"notification_type": "x", "email": "a@b.c"}))
        assert (result.ok, result.retryable) == (False, True)

    def test_a_malformed_request_is_not_retryable(self, monkeypatch):
        def raiser(*args, **kwargs):
            raise ValueError("no type")

        monkeypatch.setattr("stapel_core.notifications.publish.request_notification", raiser)
        result = deliver_notification(_context(target={"notification_type": "x", "email": "a@b.c"}))
        assert (result.ok, result.retryable) == (False, False)


class TestWsDelivery:
    def test_signals_the_stream(self, monkeypatch):
        sent = {}

        def fake_signal(stream, type, payload=None, **kwargs):
            sent.update({"stream": stream, "type": type, "payload": payload})
            return {}

        monkeypatch.setattr("stapel_core.comm.signal", fake_signal)
        result = deliver_ws(_context(target={"stream": "listings:ws:1"}))
        assert result.ok is True
        assert sent["stream"] == "listings:ws:1"
        assert sent["type"] == "listing.published"

    def test_frame_type_can_be_overridden(self, monkeypatch):
        sent = {}
        monkeypatch.setattr(
            "stapel_core.comm.signal",
            lambda stream, type, payload=None, **kw: sent.update({"type": type}) or {},
        )
        deliver_ws(_context(target={"stream": "s:ws:1", "frame_type": "custom.frame"}))
        assert sent["type"] == "custom.frame"

    def test_a_bad_stream_key_is_not_retryable(self):
        """At-most-once by contract: an authoring error fails identically
        forever, so the ladder would buy nothing."""
        result = deliver_ws(_context(target={"stream": "not a stream key"}))
        assert (result.ok, result.retryable) == (False, False)


def _handler(context):
    # The context is echoed back through the result rather than stashed on
    # the function: under importlib import mode this module exists twice
    # (as the test module and as the dotted path import_string resolves),
    # so module state is not shared between the two.
    return DeliveryResult(ok=True, detail=context.event_type)


def _refusing_handler(context):
    return DeliveryResult(ok=False, retryable=False, detail="not for me")


def _exploding_handler(context):
    raise RuntimeError("boom")


CUSTOM = "stapel_webhooks.tests.test_deliveries._handler"


class TestCustomDelivery:
    @override_settings(STAPEL_WEBHOOKS={"ALLOWED_CUSTOM_PATHS": [CUSTOM]})
    def test_allowlisted_handler_is_called_with_the_context(self):
        result = deliver_custom(_context(target={"path": CUSTOM}))
        assert result.ok is True
        assert result.detail == "listing.published"

    def test_a_path_outside_the_allowlist_is_never_imported(self):
        """Re-checked at DELIVERY time, not only at subscription time: the
        row outlives the setting that allowed it."""
        result = deliver_custom(_context(target={"path": CUSTOM}))
        assert (result.ok, result.retryable) == (False, False)
        assert "ALLOWED_CUSTOM_PATHS" in result.detail

    @override_settings(STAPEL_WEBHOOKS={"ALLOWED_CUSTOM_PATHS": ["nope.nothing.here"]})
    def test_an_unimportable_handler_is_not_retryable(self):
        result = deliver_custom(_context(target={"path": "nope.nothing.here"}))
        assert (result.ok, result.retryable) == (False, False)

    @override_settings(STAPEL_WEBHOOKS={
        "ALLOWED_CUSTOM_PATHS": ["stapel_webhooks.tests.test_deliveries._exploding_handler"]
    })
    def test_a_raising_handler_is_retryable(self):
        result = deliver_custom(
            _context(target={"path": "stapel_webhooks.tests.test_deliveries._exploding_handler"})
        )
        assert (result.ok, result.retryable) == (False, True)

    @override_settings(STAPEL_WEBHOOKS={
        "ALLOWED_CUSTOM_PATHS": ["stapel_webhooks.tests.test_deliveries._refusing_handler"]
    })
    def test_a_handler_may_drive_the_ladder_itself(self):
        result = deliver_custom(
            _context(target={"path": "stapel_webhooks.tests.test_deliveries._refusing_handler"})
        )
        assert (result.ok, result.retryable, result.detail) == (False, False, "not for me")


class TestEndToEndThroughTheRegistry:
    def test_attempt_routes_to_the_registered_handler(self, settings, transport):
        instance = transport([200])
        settings.STAPEL_WEBHOOKS = {
            "WATCH_EVENTS": ["listing.published"], "TRANSPORT": instance,
        }
        rule = services.create_subscription(
            event_type="listing.published", delivery="webhook",
            target={"url": "https://example.com/hook"},
        )
        delivery, _ = services.plan_delivery(
            rule, event_type="listing.published", event_id="e", payload={"id": "1"}
        )
        services.attempt(delivery)
        assert instance.calls[0]["url"] == "https://example.com/hook"
