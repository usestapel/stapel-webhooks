def pytest_configure(config):
    from django.conf import settings
    if not settings.configured:
        # Single source of truth for this block lives in
        # _codegen_settings.py so the test harness and any emission harness
        # can never drift.
        from stapel_webhooks._codegen_settings import settings_kwargs

        settings.configure(**settings_kwargs())
        import django
        django.setup()

        from stapel_core.comm.schemas import autoload_schemas
        autoload_schemas()


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_registries():
    """Runtime delivery types, runtime watched topics and the catalog cache
    are process-global by design — reset them between tests so one test's
    registration never leaks into the next."""
    from stapel_webhooks import actions, catalog, registry

    yield
    registry.reset_delivery_types()
    actions.reset_runtime_events()
    catalog.reset_catalog()


@pytest.fixture
def api_client():
    from rest_framework.test import APIClient
    return APIClient()


@pytest.fixture
def user(db):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create(username="owner", email="owner@example.com")


@pytest.fixture
def other_user(db):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create(username="stranger", email="stranger@example.com")


@pytest.fixture
def staff_user(db):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create(
        username="operator", email="operator@example.com", is_staff=True
    )


@pytest.fixture
def authed_client(user):
    from rest_framework.test import APIClient

    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def watch_any_event(settings):
    """Let subscriptions name the test topics.

    The real vocabulary comes from installed modules' ``schemas/emits/``;
    in this suite only stapel-webhooks' own are installed, so tests declare
    what they emit through the same door a host would use.
    """
    settings.STAPEL_WEBHOOKS = {
        **getattr(settings, "STAPEL_WEBHOOKS", {}),
        "WATCH_EVENTS": ["listing.published", "user.registered", "test.event"],
    }
    return settings.STAPEL_WEBHOOKS["WATCH_EVENTS"]


class RecordingTransport:
    """A transport that records instead of dialling.

    Registered by the ``transport`` fixture. ``responses`` is a list of
    statuses (or exceptions) to answer with, one per call, the last one
    repeating — enough to script an outage and its recovery.
    """

    def __init__(self, responses=None):
        self.calls = []
        self.responses = list(responses or [200])

    def post(self, url, body, headers=None, **kwargs):
        from stapel_webhooks.transport import TransportResponse

        self.calls.append({"url": url, "body": body, "headers": dict(headers or {})})
        answer = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
        if isinstance(answer, Exception):
            raise answer
        return TransportResponse(status=int(answer), body="", headers={})

    def classify(self, status):
        from stapel_webhooks.transport import SafeHttpsTransport

        return SafeHttpsTransport().classify(status)


@pytest.fixture
def transport(settings):
    """Install a :class:`RecordingTransport` for the duration of a test."""

    def _install(responses=None):
        instance = RecordingTransport(responses)
        settings.STAPEL_WEBHOOKS = {
            **getattr(settings, "STAPEL_WEBHOOKS", {}),
            "TRANSPORT": instance,
        }
        return instance

    return _install
