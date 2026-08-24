"""The REST surface: authoring rules, reading evidence, replaying a letter."""
import pytest

from stapel_webhooks import services
from stapel_webhooks.models import STATUS_DEAD, Delivery, Subscription

pytestmark = pytest.mark.django_db

BASE = "/webhooks/api/v1"


@pytest.fixture(autouse=True)
def _watched(watch_any_event, transport):
    transport()


def _create(client, **overrides):
    body = {
        "event_type": "listing.published",
        "delivery": "webhook",
        "target": {"url": "https://example.com/hook"},
    }
    body.update(overrides)
    return client.post(f"{BASE}/subscriptions", body, format="json")


class TestAuthentication:
    def test_every_route_requires_a_user(self, api_client):
        for path in ("subscriptions", "event-catalog"):
            assert api_client.get(f"{BASE}/{path}").status_code in (401, 403)


class TestSubscriptionCreate:
    def test_create_returns_the_secret_exactly_once(self, authed_client):
        response = _create(authed_client)
        assert response.status_code == 201
        assert response.json()["secret"]
        subscription_id = response.json()["id"]

        read = authed_client.get(f"{BASE}/subscriptions/{subscription_id}")
        assert read.status_code == 200
        assert "secret" not in read.json()
        assert read.json()["has_secret"] is True

    def test_unknown_event_is_refused(self, authed_client):
        response = _create(authed_client, event_type="nothing.emits.this")
        assert response.status_code == 400
        assert response.json()["localizable_error"] == "error.400.webhooks_unknown_event"

    def test_unknown_delivery_type_is_refused(self, authed_client):
        response = _create(authed_client, delivery="carrier-pigeon")
        assert response.json()["localizable_error"] == "error.400.webhooks_unknown_delivery"

    def test_plaintext_target_is_refused(self, authed_client):
        response = _create(authed_client, target={"url": "http://example.com/hook"})
        assert response.json()["localizable_error"] == "error.400.webhooks_insecure_target"

    def test_bad_target_shape_is_refused(self, authed_client):
        response = _create(authed_client, target={})
        assert response.json()["localizable_error"] == "error.400.webhooks_invalid_target"

    def test_bad_filter_is_refused(self, authed_client):
        response = _create(authed_client, filter={"a": {"$regex": ".*"}})
        assert response.json()["localizable_error"] == "error.400.webhooks_invalid_filter"

    def test_custom_delivery_is_closed_by_default(self, authed_client):
        """The dotted-path type is unusable until a host allowlists handlers."""
        response = _create(authed_client, delivery="custom", target={"path": "os.system"})
        assert response.json()["localizable_error"] == "error.400.webhooks_invalid_target"

    def test_cap_is_enforced(self, authed_client, settings):
        settings.STAPEL_WEBHOOKS = {
            **settings.STAPEL_WEBHOOKS, "MAX_SUBSCRIPTIONS_PER_OWNER": 1,
        }
        assert _create(authed_client).status_code == 201
        assert _create(authed_client).json()["localizable_error"] == "error.409.webhooks_subscription_cap"


class TestSubscriptionScoping:
    def test_a_stranger_sees_a_404_not_a_403(self, authed_client, other_user, api_client):
        """The id is not public; "exists but not yours" is an enumeration
        oracle for other tenants' rule ids."""
        subscription_id = _create(authed_client).json()["id"]
        api_client.force_authenticate(user=other_user)
        assert api_client.get(f"{BASE}/subscriptions/{subscription_id}").status_code == 404

    def test_the_list_is_scoped_to_the_caller(self, authed_client, other_user, api_client):
        _create(authed_client)
        api_client.force_authenticate(user=other_user)
        assert api_client.get(f"{BASE}/subscriptions").json() == []
        assert len(authed_client.get(f"{BASE}/subscriptions").json()) == 1

    def test_staff_see_everything(self, authed_client, staff_user, api_client):
        subscription_id = _create(authed_client).json()["id"]
        api_client.force_authenticate(user=staff_user)
        assert api_client.get(f"{BASE}/subscriptions/{subscription_id}").status_code == 200


class TestSubscriptionEdit:
    def test_patch_revalidates_the_whole_rule(self, authed_client):
        subscription_id = _create(authed_client).json()["id"]
        response = authed_client.patch(
            f"{BASE}/subscriptions/{subscription_id}",
            {"target": {}},
            format="json",
        )
        assert response.json()["localizable_error"] == "error.400.webhooks_invalid_target"

    def test_patch_updates_the_filter_under_its_wire_name(self, authed_client):
        subscription_id = _create(authed_client).json()["id"]
        response = authed_client.patch(
            f"{BASE}/subscriptions/{subscription_id}",
            {"filter": {"city": "berlin"}, "description": "CRM"},
            format="json",
        )
        assert response.status_code == 200
        assert response.json()["filter"] == {"city": "berlin"}
        assert Subscription.objects.get().payload_filter == {"city": "berlin"}

    def test_rotate_issues_a_new_secret(self, authed_client):
        created = _create(authed_client).json()
        rotated = authed_client.post(f"{BASE}/subscriptions/{created['id']}/secret")
        assert rotated.status_code == 200
        assert rotated.json()["secret"] != created["secret"]

    def test_rotating_an_unsigned_type_is_refused(self, authed_client, monkeypatch):
        subscription_id = _create(
            authed_client, delivery="ws", target={"stream": "listings:ws:1"}
        ).json()["id"]
        response = authed_client.post(f"{BASE}/subscriptions/{subscription_id}/secret")
        assert response.json()["localizable_error"] == "error.400.webhooks_not_signed_type"

    def test_delete(self, authed_client):
        subscription_id = _create(authed_client).json()["id"]
        assert authed_client.delete(f"{BASE}/subscriptions/{subscription_id}").status_code == 204
        assert Subscription.objects.count() == 0


class TestDeliveries:
    @pytest.fixture
    def dead_delivery(self, authed_client, user):
        subscription_id = _create(authed_client).json()["id"]
        rule = Subscription.objects.get(pk=subscription_id)
        delivery, _ = services.plan_delivery(
            rule, event_type="listing.published", event_id="e1", payload={"id": "1"}
        )
        Delivery.objects.filter(pk=delivery.pk).update(status=STATUS_DEAD, attempts=8)
        delivery.refresh_from_db()
        return rule, delivery

    def test_the_log_of_a_rule_is_readable(self, authed_client, dead_delivery):
        rule, delivery = dead_delivery
        response = authed_client.get(f"{BASE}/subscriptions/{rule.id}/deliveries")
        assert response.status_code == 200
        assert response.json()[0]["id"] == str(delivery.id)
        assert response.json()[0]["status"] == "dead"

    def test_the_log_filters_by_status(self, authed_client, dead_delivery):
        rule, _ = dead_delivery
        assert authed_client.get(f"{BASE}/subscriptions/{rule.id}/deliveries?status=succeeded").json() == []

    def test_a_delivery_carries_the_payload_a_replay_would_send(self, authed_client, dead_delivery):
        _, delivery = dead_delivery
        body = authed_client.get(f"{BASE}/deliveries/{delivery.id}").json()
        assert body["payload"] == {"id": "1"}
        assert body["attempts"] == 8

    def test_replay_requeues_from_attempt_zero(self, authed_client, dead_delivery):
        _, delivery = dead_delivery
        response = authed_client.post(f"{BASE}/deliveries/{delivery.id}/replay")
        assert response.status_code == 200
        assert response.json()["status"] == "pending"
        delivery.refresh_from_db()
        assert delivery.attempts == 0

    def test_replaying_a_live_delivery_is_refused(self, authed_client, dead_delivery):
        _, delivery = dead_delivery
        authed_client.post(f"{BASE}/deliveries/{delivery.id}/replay")
        response = authed_client.post(f"{BASE}/deliveries/{delivery.id}/replay")
        assert response.json()["localizable_error"] == "error.409.webhooks_not_replayable"

    def test_a_strangers_delivery_is_a_404(self, authed_client, dead_delivery, other_user, api_client):
        _, delivery = dead_delivery
        api_client.force_authenticate(user=other_user)
        assert api_client.get(f"{BASE}/deliveries/{delivery.id}").status_code == 404


class TestEventCatalog:
    def test_lists_what_is_subscribable_and_how(self, authed_client):
        body = authed_client.get(f"{BASE}/event-catalog").json()
        names = [entry["event"] for entry in body["events"]]
        assert "listing.published" in names
        assert "webhook" in body["delivery_types"]
        assert names == sorted(names)

    def test_own_events_are_absent(self, authed_client):
        body = authed_client.get(f"{BASE}/event-catalog").json()
        assert "webhooks.delivery.dead" not in [e["event"] for e in body["events"]]

    def test_a_shipped_schema_carries_its_shape(self, authed_client, settings):
        settings.STAPEL_WEBHOOKS = {**settings.STAPEL_WEBHOOKS, "IGNORE_EVENTS": []}
        body = authed_client.get(f"{BASE}/event-catalog").json()
        entry = next(e for e in body["events"] if e["event"] == "webhooks.delivery.dead")
        assert entry["module"] == "webhooks"
        assert "subscription_id" in entry["properties"]


class TestErrorKeys:
    def test_the_collector_endpoint_lists_owned_keys(self, authed_client):
        response = authed_client.get(f"{BASE}/error-keys/")
        assert response.status_code in (200, 403)
