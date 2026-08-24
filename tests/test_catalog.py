"""``event_catalog()`` — subscribable because installed, not because listed."""
import json

from django.test import override_settings

from stapel_webhooks import catalog
from stapel_webhooks.actions import watched_events


class TestCatalog:
    def test_reads_installed_apps_emits_schemas(self):
        """This package ships three emits; they must appear without anything
        registering them anywhere."""
        found = catalog.event_catalog(refresh=True)
        assert "webhooks.delivery.dead" in found
        entry = found["webhooks.delivery.dead"]
        assert entry["module"] == "webhooks"
        assert entry["package"] == "stapel_webhooks"
        assert "subscription_id" in entry["properties"]
        assert "delivery_id" in entry["required"]
        assert entry["description"]

    def test_schema_is_carried_verbatim(self):
        schema = catalog.event_schema("webhooks.delivery.succeeded")
        assert schema["title"] == "webhooks.delivery.succeeded"
        assert schema["type"] == "object"

    def test_names_are_sorted_and_queryable(self):
        catalog.reset_catalog()
        names = catalog.catalog_event_names()
        assert names == sorted(names)
        assert catalog.is_known_event("webhooks.delivery.dead") is True
        assert catalog.is_known_event("nothing.at.all") is False

    def test_cache_is_per_process_and_resettable(self):
        first = catalog.event_catalog(refresh=True)
        assert catalog.event_catalog() is first
        catalog.reset_catalog()
        assert catalog.event_catalog() is not first

    def test_extra_paths_let_a_non_app_library_contribute(self, tmp_path):
        emits = tmp_path / "schemas" / "emits"
        emits.mkdir(parents=True)
        (emits / "vendor.thing.happened.json").write_text(
            json.dumps({"title": "vendor.thing.happened", "properties": {"id": {"type": "string"}}})
        )
        with override_settings(STAPEL_WEBHOOKS={"EXTRA_CATALOG_PATHS": [str(tmp_path)]}):
            found = catalog.event_catalog(refresh=True)
            assert "vendor.thing.happened" in found
            assert found["vendor.thing.happened"]["properties"] == ["id"]

    def test_unreadable_schema_is_skipped_not_fatal(self, tmp_path):
        emits = tmp_path / "schemas" / "emits"
        emits.mkdir(parents=True)
        (emits / "broken.event.json").write_text("{not json")
        (emits / "good.event.json").write_text(json.dumps({"properties": {}}))
        with override_settings(STAPEL_WEBHOOKS={"EXTRA_CATALOG_PATHS": [str(tmp_path)]}):
            found = catalog.event_catalog(refresh=True)
            assert "broken.event" not in found
            assert "good.event" in found


class TestWatchedVocabulary:
    def test_catalog_feeds_the_watch_set(self):
        catalog.reset_catalog()
        with override_settings(STAPEL_WEBHOOKS={"IGNORE_EVENTS": []}):
            assert "webhooks.delivery.dead" in watched_events()

    def test_own_events_are_ignored_by_default(self):
        """The reaction layer does not react to itself: a dead-letter about
        a dead-letter is a loop bounded only by the retry ladder."""
        catalog.reset_catalog()
        watched = watched_events()
        assert "webhooks.delivery.dead" not in watched
        assert "webhooks.subscription.disabled" not in watched

    @override_settings(STAPEL_WEBHOOKS={"WATCH_CATALOG": False, "WATCH_EVENTS": ["only.this"]})
    def test_catalog_can_be_switched_off(self):
        catalog.reset_catalog()
        assert watched_events() == frozenset({"only.this"})

    @override_settings(STAPEL_WEBHOOKS={"WATCH_CATALOG": False, "WATCH_EVENTS": []})
    def test_empty_watch_set_is_possible_and_is_what_w001_reports(self):
        catalog.reset_catalog()
        assert watched_events() == frozenset()
