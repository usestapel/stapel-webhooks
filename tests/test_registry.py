"""The delivery-type merge-registry — the semantics, not the built-ins.

What is asserted here is the LAYERING (built-ins <- settings <- runtime,
None removes), because that is the property a host relies on when it closes
``custom`` or ships its own delivery kind. The four built-ins are asserted
only as "present and shaped like a spec": which four they are is a design
decision, and pinning them harder would make adding a fifth a test edit.
"""
import pytest
from django.test import override_settings

from stapel_webhooks import registry


class TestBuiltins:
    def test_the_four_deliveries_are_registered(self):
        types = registry.get_delivery_types()
        assert {"webhook", "notification", "ws", "custom"} <= set(types)

    def test_every_spec_names_a_handler(self):
        for name, spec in registry.get_delivery_types().items():
            assert spec.get("handler"), name
            assert registry.delivery_handler(name) is not None

    def test_only_webhook_is_signed_and_external(self):
        assert registry.is_signed("webhook") is True
        assert registry.is_external("webhook") is True
        for name in ("notification", "ws", "custom"):
            assert registry.is_signed(name) is False
            assert registry.is_external(name) is False


class TestMergeSemantics:
    @override_settings(STAPEL_WEBHOOKS={"DELIVERY_TYPES": {"slack": {"handler": "x.y"}}})
    def test_settings_add_a_type(self):
        assert "slack" in registry.get_delivery_types()

    @override_settings(STAPEL_WEBHOOKS={"DELIVERY_TYPES": {"custom": None}})
    def test_settings_none_removes_a_builtin(self):
        types = registry.get_delivery_types()
        assert "custom" not in types
        assert "webhook" in types
        with pytest.raises(registry.UnknownDeliveryType):
            registry.resolve_delivery("custom")

    @override_settings(STAPEL_WEBHOOKS={"DELIVERY_TYPES": {"webhook": {"handler": "a.b", "signed": False}}})
    def test_settings_override_a_builtin_wholesale(self):
        """A spec REPLACES, it does not deep-merge — a half-overridden spec
        would silently keep keys the host meant to drop."""
        spec = registry.resolve_delivery("webhook")
        assert spec == {"handler": "a.b", "signed": False}
        assert registry.is_signed("webhook") is False

    def test_runtime_beats_settings(self):
        with override_settings(STAPEL_WEBHOOKS={"DELIVERY_TYPES": {"x": {"handler": "a.b"}}}):
            registry.register_delivery_type("x", {"handler": "c.d"})
            assert registry.resolve_delivery("x")["handler"] == "c.d"

    def test_runtime_none_removes_a_builtin(self):
        registry.register_delivery_type("ws", None)
        assert "ws" not in registry.get_delivery_types()

    def test_reset_restores_the_builtins(self):
        registry.register_delivery_type("ws", None)
        registry.reset_delivery_types()
        assert "ws" in registry.get_delivery_types()


class TestTargetValidation:
    def test_missing_required_key_is_refused(self):
        with pytest.raises(registry.InvalidTarget):
            registry.validate_target("webhook", {})

    def test_url_target_is_accepted(self):
        registry.validate_target("webhook", {"url": "https://example.com/hook"})

    def test_notification_needs_an_addressee(self):
        with pytest.raises(registry.InvalidTarget):
            registry.validate_target("notification", {"notification_type": "x"})
        registry.validate_target("notification", {"notification_type": "x", "email": "a@b.c"})

    def test_non_object_target_is_refused(self):
        with pytest.raises(registry.InvalidTarget):
            registry.validate_target("ws", ["stream"])

    def test_custom_path_must_be_allowlisted(self):
        """The allowlist ships EMPTY: a dotted path in a row is code chosen
        by data, so nothing is callable until a host names it."""
        with pytest.raises(registry.InvalidTarget):
            registry.validate_target("custom", {"path": "os.system"})
        with override_settings(
            STAPEL_WEBHOOKS={"ALLOWED_CUSTOM_PATHS": ["myapp.handlers.on_event"]}
        ):
            registry.validate_target("custom", {"path": "myapp.handlers.on_event"})
            with pytest.raises(registry.InvalidTarget):
                registry.validate_target("custom", {"path": "os.system"})

    def test_unknown_type_raises(self):
        with pytest.raises(registry.UnknownDeliveryType):
            registry.validate_target("carrier-pigeon", {})
