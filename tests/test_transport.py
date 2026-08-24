"""The SSRF guard on the outbound leg.

A webhook target is the most hostile URL a deployment holds: supplied by
whoever made the subscription, dialled by a background worker, retried. Each
refusal below is a real attack shape, and each is asserted to be
NON-retryable — a blocked address must dead-letter, not occupy the ladder
for two hours first.
"""
import pytest
from django.test import override_settings

from stapel_webhooks.transport import SafeHttpsTransport, TransportError


@pytest.fixture
def transport_instance():
    return SafeHttpsTransport()


class TestUrlPolicy:
    def test_plaintext_http_is_refused(self, transport_instance):
        with pytest.raises(TransportError) as exc:
            transport_instance.post("http://example.com/hook", b"{}")
        assert exc.value.code == "scheme_not_https"
        assert exc.value.retryable is False

    @pytest.mark.parametrize("url", [
        "file:///etc/passwd",
        "gopher://example.com/",
        "ftp://example.com/",
    ])
    def test_non_http_schemes_are_refused(self, transport_instance, url):
        with pytest.raises(TransportError) as exc:
            transport_instance.post(url, b"{}")
        assert exc.value.code == "scheme_unsupported"

    def test_a_url_without_a_host_is_refused(self, transport_instance):
        with pytest.raises(TransportError) as exc:
            transport_instance.post("https:///hook", b"{}")
        assert exc.value.code == "no_host"

    @override_settings(STAPEL_WEBHOOKS={"ALLOWED_TARGET_HOSTS": ["receiver.internal"]})
    def test_host_allowlist_is_exact(self, transport_instance):
        with pytest.raises(TransportError) as exc:
            transport_instance.post("https://evil.example.com/hook", b"{}")
        assert exc.value.code == "host_not_allowed"
        assert exc.value.retryable is False


class TestAddressPolicy:
    @pytest.mark.parametrize("url", [
        "https://127.0.0.1/hook",
        "https://10.0.0.5/hook",
        "https://192.168.1.1/hook",
        "https://169.254.169.254/latest/meta-data/",  # cloud metadata
        "https://[::1]/hook",
    ])
    def test_private_and_metadata_addresses_are_blocked(self, transport_instance, url):
        with pytest.raises(TransportError) as exc:
            transport_instance.post(url, b"{}")
        assert exc.value.code == "blocked_ip"
        assert exc.value.retryable is False

    def test_an_unresolvable_host_is_retryable(self, transport_instance):
        """DNS is weather, not policy."""
        with pytest.raises(TransportError) as exc:
            transport_instance.post("https://no-such-host.invalid/hook", b"{}")
        assert exc.value.code == "dns_resolution_failed"
        assert exc.value.retryable is True

    @override_settings(STAPEL_WEBHOOKS={"ALLOW_INSECURE_TARGETS": True})
    def test_the_confession_switch_opens_both_doors(self, transport_instance, monkeypatch):
        """With ALLOW_INSECURE_TARGETS the guard is off — plaintext and
        loopback both reach the connection step. Asserted through the
        connect seam so the test dials nothing."""
        attempts = []

        def fake_connect(scheme, host, target, port, *, timeout):
            attempts.append((scheme, host, target, port))
            raise TransportError("connection_failed", "no server here")

        monkeypatch.setattr(transport_instance, "_connect", fake_connect)
        with pytest.raises(TransportError):
            transport_instance.post("http://127.0.0.1:8080/hook", b"{}")
        assert attempts == [("http", "127.0.0.1", "127.0.0.1", 8080)]


class TestResponseHandling:
    def test_redirects_are_never_followed(self, transport_instance, monkeypatch):
        """A signed POST is not replayed at an address the subscription's
        owner never named."""

        class FakeResponse:
            status = 302

            def read(self, n):
                return b""

            def getheaders(self):
                return []

        class FakeConn:
            def request(self, *a, **kw):
                pass

            def getresponse(self):
                return FakeResponse()

            def close(self):
                pass

        monkeypatch.setattr(transport_instance, "_connect", lambda *a, **kw: FakeConn())
        with override_settings(STAPEL_WEBHOOKS={"ALLOW_INSECURE_TARGETS": True}):
            with pytest.raises(TransportError) as exc:
                transport_instance.post("https://example.com/hook", b"{}")
        assert exc.value.code == "redirect_not_followed"
        assert exc.value.retryable is False

    def test_the_response_body_is_capped(self, transport_instance, monkeypatch):
        class FakeResponse:
            status = 500

            def read(self, n):
                return b"x" * n

            def getheaders(self):
                return [("Content-Type", "text/plain")]

        class FakeConn:
            def request(self, *a, **kw):
                pass

            def getresponse(self):
                return FakeResponse()

            def close(self):
                pass

        monkeypatch.setattr(transport_instance, "_connect", lambda *a, **kw: FakeConn())
        with override_settings(
            STAPEL_WEBHOOKS={"ALLOW_INSECURE_TARGETS": True, "MAX_RESPONSE_BYTES": 16}
        ):
            response = transport_instance.post("https://example.com/hook", b"{}")
        assert len(response.body) == 16


class TestClassification:
    @pytest.mark.parametrize("status,expected", [
        (200, (True, False)),
        (204, (True, False)),
        (400, (False, False)),
        (401, (False, False)),
        (404, (False, False)),
        (408, (False, True)),
        (429, (False, True)),
        (500, (False, True)),
        (503, (False, True)),
    ])
    def test_only_transient_statuses_earn_the_ladder(self, transport_instance, status, expected):
        assert transport_instance.classify(status) == expected
