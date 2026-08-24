"""The committed contract: schemas, comm surface, settings hygiene.

These are the assertions that fail when a refactor drifts from the artifacts
other people integrate against — the emitted payloads against their JSON
schemas, the comm Function names, and the promise that no secret rides an
event.
"""
import json
from pathlib import Path

import pytest
from django.db import transaction
from stapel_core.comm import call, on_action

from stapel_webhooks import events, services
from stapel_webhooks.models import STATUS_DEAD

pytestmark = pytest.mark.django_db

ROOT = Path(__file__).resolve().parent.parent


class TestShippedSchemas:
    def test_every_emit_helper_has_a_schema_file(self):
        shipped = {p.stem for p in (ROOT / "schemas" / "emits").glob("*.json")}
        assert shipped == set(events.EMITTED_EVENTS)

    def test_every_function_has_a_schema_file(self):
        shipped = {p.stem for p in (ROOT / "schemas" / "functions").glob("*.json")}
        assert shipped == {"webhooks.event_catalog", "webhooks.dispatch"}

    def test_schema_files_are_named_after_their_title(self):
        for path in (ROOT / "schemas").glob("**/*.json"):
            schema = json.loads(path.read_text())
            assert schema["title"] == path.stem, path

    def test_emitted_payloads_validate(self, watch_any_event, transport, settings):
        """VALIDATE_SCHEMAS is on in this suite, so an emit whose payload
        drifts from its committed schema raises here rather than at a
        consumer."""
        transport([400])
        settings.STAPEL_WEBHOOKS = {
            **settings.STAPEL_WEBHOOKS, "DISABLE_AFTER_DEAD": 1,
        }
        seen = []
        for name in events.EMITTED_EVENTS:
            on_action(name, schema=None)(seen.append)
        rule = services.create_subscription(
            event_type="listing.published", delivery="webhook",
            target={"url": "https://example.com/hook"},
        )
        delivery, _ = services.plan_delivery(
            rule, event_type="listing.published", event_id="e", payload={"id": "1"}
        )
        services.attempt(delivery)
        assert delivery.status == STATUS_DEAD
        assert {event.event_type for event in seen} == {
            "webhooks.delivery.dead", "webhooks.subscription.disabled",
        }

    def test_no_secret_ever_rides_an_event(self, watch_any_event, transport):
        transport([200])
        seen = []
        on_action("webhooks.delivery.succeeded", schema=None)(seen.append)
        rule = services.create_subscription(
            event_type="listing.published", delivery="webhook",
            target={"url": "https://example.com/hook"},
        )
        delivery, _ = services.plan_delivery(
            rule, event_type="listing.published", event_id="e", payload={"id": "1"}
        )
        services.attempt(delivery)
        body = json.dumps(seen[0].payload)
        assert rule.secret not in body
        assert "url" not in body


class TestCommSurface:
    def test_event_catalog_function(self):
        answer = call("webhooks.event_catalog", {"refresh": True})
        names = [entry["event"] for entry in answer["events"]]
        assert "webhooks.delivery.dead" in names
        assert names == sorted(names)
        # A picker's vocabulary, not a schema dump.
        assert "schema" not in answer["events"][0]

    def test_dispatch_function_plans_and_is_idempotent(self, watch_any_event, transport):
        transport()
        services.create_subscription(
            event_type="listing.published", delivery="webhook",
            target={"url": "https://example.com/hook"},
        )
        payload = {"event_type": "listing.published", "event_id": "x1", "payload": {"id": "1"}}
        with transaction.atomic():
            assert call("webhooks.dispatch", payload) == {"planned": 1}
            assert call("webhooks.dispatch", payload) == {"planned": 0}


class TestSettingsHygiene:
    def test_the_seams_are_env_closed(self):
        """A key that names the code the process loads is never read from an
        environment variable (stapel_core.conf)."""
        from stapel_webhooks.conf import webhooks_settings

        closed = set(webhooks_settings.env_closed_keys())
        assert {"TRANSPORT", "SIGNER", "MATCHER"} <= closed

    def test_every_default_is_documented_in_config_md(self):
        text = (ROOT / "CONFIG.MD").read_text()
        from stapel_webhooks.conf import DEFAULTS

        missing = [key for key in DEFAULTS if key not in text]
        assert missing == []


class TestMountRecipe:
    def test_the_module_bakes_in_the_api_v1_segment(self):
        """api-versioning.md §2: a host mounts ``webhooks/`` and gets
        ``/webhooks/api/v1/…``."""
        from django.urls import reverse

        assert reverse("webhooks-subscriptions") == "/webhooks/api/v1/subscriptions"


class TestErrorCatalogues:
    """Owning a key means shipping its catalogues (i18n-shipping.md §4).

    A key registered without a translation renders as the raw key in every
    non-English client — which looks exactly like a bug in the client.
    """

    def test_every_owned_key_is_translated(self):
        from stapel_webhooks.errors import STAPEL_WEBHOOKS_ERRORS

        for language in ("ru", "es"):
            catalogue = json.loads(
                (ROOT / "translations" / f"errors.{language}.json").read_text()
            )
            assert set(catalogue) == set(STAPEL_WEBHOOKS_ERRORS), language

    def test_placeholders_survive_translation(self):
        """A translated string that drops {event_type} silently loses the
        one piece of information the message carries."""
        import re

        from stapel_webhooks.errors import STAPEL_WEBHOOKS_ERRORS

        def slots(text):
            return set(re.findall(r"{(\w+)}", text))

        for language in ("ru", "es"):
            catalogue = json.loads(
                (ROOT / "translations" / f"errors.{language}.json").read_text()
            )
            for key, english in STAPEL_WEBHOOKS_ERRORS.items():
                assert slots(catalogue[key]) == slots(english), (language, key)

    def test_every_key_declares_a_remediation(self):
        from stapel_webhooks.errors import (
            STAPEL_WEBHOOKS_ERRORS,
            STAPEL_WEBHOOKS_REMEDIATION,
        )

        assert set(STAPEL_WEBHOOKS_REMEDIATION) == set(STAPEL_WEBHOOKS_ERRORS)
