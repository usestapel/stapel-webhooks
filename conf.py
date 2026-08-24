"""Settings namespace for stapel-webhooks.

All configuration is read through ``webhooks_settings`` (lazily, at call
time) — never via module-level ``os.getenv`` (values would freeze at import).
Resolution order per key: ``settings.STAPEL_WEBHOOKS`` dict -> flat Django
setting of the same name -> environment variable -> the default below.

The extension seams are two, and they are different in kind:

- ``DELIVERY_TYPES`` — the MERGE-registry (``registry.py``): built-ins
  ``webhook`` / ``notification`` / ``ws`` / ``custom``, merged over by this
  key, merged over by runtime ``register_delivery_type()``. ``None`` removes
  a type, which is how a host closes ``custom`` (arbitrary dotted-path
  execution) without forking anything.
- ``TRANSPORT`` / ``SIGNER`` / ``MATCHER`` — dotted paths resolved by
  ``import_string``. They are ``import_strings`` members, so the environment
  can never choose them (``stapel_core.conf``: a name that decides which
  code runs is not read from a variable anything in the pod can set).

**Every switch that trades safety for reach ships CLOSED.** The outbound
target must be https and must resolve to a public address
(``ALLOW_INSECURE_TARGETS = False``); delivery does not happen on the
request thread (``DISPATCH_MODE = "deferred"``); a subscription that keeps
dead-lettering disables itself. Opening any of them is an explicit host
decision, and the two that matter print a system check saying so.
"""
from stapel_core.conf import AppSettings

#: AppSettings-shaped literal dict (capability-config.md §2): a top-level
#: DEFAULTS lets the capabilities.json emitter introspect axis keys/kinds
#: without re-parsing the AppSettings() call.
DEFAULTS = {
    # ── Delivery registry (registry.py) ──────────────────────────────
    # {type_name: spec dict | None}, merged OVER BUILTIN_DELIVERY_TYPES.
    # None removes a type. See registry.resolve_delivery for the key set.
    "DELIVERY_TYPES": {},

    # ── What the reaction layer listens to ───────────────────────────
    # Explicit topics to wire, on top of whatever the catalog contributes.
    # A host that emits an event no installed package declares a schema for
    # names it here.
    "WATCH_EVENTS": [],
    # Also wire every event named by an installed package's
    # ``schemas/emits/`` (catalog.event_catalog()). This is what makes
    # "subscribable" a property of what is installed rather than of a list
    # somebody maintains by hand.
    "WATCH_CATALOG": True,
    # Topics the reaction layer refuses to react to, whatever the catalog
    # says. This module's own delivery facts are here by default: a
    # subscription on webhooks.delivery.dead whose own delivery dies emits
    # another one, and the loop is only bounded by the retry ladder.
    "IGNORE_EVENTS": [
        "webhooks.delivery.succeeded",
        "webhooks.delivery.dead",
        "webhooks.subscription.disabled",
    ],

    # ── Dispatch ─────────────────────────────────────────────────────
    # "deferred" (default): matching an event writes Delivery rows and
    # returns; the drain (management command / beat task) performs the
    # network calls. "inline": deliver right after commit, in the emitting
    # process. Inline is a dev/monolith convenience — it puts a stranger's
    # HTTP endpoint on the request thread's critical path, so it is not the
    # default and webhooks.W002 says so.
    "DISPATCH_MODE": "deferred",
    # Rows one drain pass claims.
    "DRAIN_BATCH_SIZE": 100,
    # How long a claimed row stays invisible to other drains. The claim is a
    # conditional UPDATE that pushes next_attempt_at forward, so it works on
    # every backend (SELECT ... SKIP LOCKED does not) and a worker that dies
    # mid-attempt releases its row by the clock instead of by a lock nobody
    # will ever unlock.
    "CLAIM_LEASE_SECONDS": 120,
    "DRAIN_SCHEDULE": {"minute": "*"},

    # ── Retry ladder / dead-letter ───────────────────────────────────
    # Attempts before a delivery is dead-lettered. A permanently refused
    # delivery (a 4xx that is not 408/425/429) skips the ladder entirely.
    "MAX_ATTEMPTS": 8,
    # backoff = min(BACKOFF_BASE_SECONDS * FACTOR ** (attempt - 1), CAP),
    # then +/- JITTER_RATIO. With the defaults the ladder spans ~2h before
    # the dead-letter, which is the outage a receiver can plausibly have.
    "BACKOFF_BASE_SECONDS": 10,
    "BACKOFF_FACTOR": 3.0,
    "BACKOFF_CAP_SECONDS": 3600,
    "JITTER_RATIO": 0.1,
    # Consecutive dead-letters after which a subscription is deactivated
    # and webhooks.subscription.disabled is emitted. 0 disables the guard —
    # a receiver that has been gone for a week keeps being called forever.
    "DISABLE_AFTER_DEAD": 5,

    # ── Outbound HTTP (webhook delivery) ─────────────────────────────
    "TIMEOUT_SECONDS": 10.0,
    "TOTAL_DEADLINE_SECONDS": 20.0,
    # A receiver's response body is diagnostics, not data: enough to put the
    # remote's error in last_error, never enough to be a memory lever.
    "MAX_RESPONSE_BYTES": 4096,
    # A payload above this is dead-lettered at planning time rather than
    # pushed at a stranger.
    "MAX_PAYLOAD_BYTES": 262144,
    "USER_AGENT": "stapel-webhooks/1.0",
    # Exact-host allowlist applied to every webhook target. Empty = any
    # public https host (the SSRF guard still applies).
    "ALLOWED_TARGET_HOSTS": [],
    # The confession switch (security canon H10). True permits http:// and
    # targets resolving to private/loopback/link-local addresses — i.e. it
    # turns the SSRF guard off for a dev box or an in-cluster receiver.
    # Silence is not a refusal, so leaving it on prints webhooks.W001.
    "ALLOW_INSECURE_TARGETS": False,

    # ── Signature ────────────────────────────────────────────────────
    "SIGNATURE_HEADER": "X-Stapel-Signature",
    "SIGNATURE_SCHEME": "v1",
    # How far a receiver may let a signed timestamp drift before it treats
    # the request as a replay. Published in MODULE.md because it is the
    # receiver's setting as much as ours.
    "SIGNATURE_TOLERANCE_SECONDS": 300,
    "SECRET_BYTES": 32,

    # ── Retention ────────────────────────────────────────────────────
    # Succeeded deliveries are a log; dead ones are evidence and are kept
    # for the longer horizon (None = forever).
    "SUCCEEDED_RETENTION_DAYS": 7,
    "DEAD_RETENTION_DAYS": 90,
    "PURGE_SCHEDULE": {"hour": 4, "minute": 20},

    # ── API surface ──────────────────────────────────────────────────
    "MAX_PAGE_SIZE": 100,
    "MAX_SUBSCRIPTIONS_PER_OWNER": 100,
    # Widest JSON-predicate nesting accepted on a subscription filter.
    "MAX_FILTER_DEPTH": 4,

    # ── custom delivery ──────────────────────────────────────────────
    # Dotted paths the ``custom`` delivery type may name. An ALLOWLIST, and
    # it ships EMPTY: a subscription row naming a dotted path is arbitrary
    # in-process code selected by data, so until a host says which callables
    # are handlers, none are. Closing the type entirely is the other half of
    # the same control: DELIVERY_TYPES = {"custom": None}.
    "ALLOWED_CUSTOM_PATHS": [],

    # ── Catalog ──────────────────────────────────────────────────────
    # Extra directories scanned for ``schemas/emits/*.json`` on top of every
    # installed app's own. This is how an L1 library (no AppConfig, so no
    # app path) contributes its events.
    "EXTRA_CATALOG_PATHS": [],

    # ── Seams (dotted paths; never read from the environment) ────────
    "TRANSPORT": "stapel_webhooks.transport.SafeHttpsTransport",
    "SIGNER": "stapel_webhooks.signing.HmacSha256Signer",
    "MATCHER": "stapel_webhooks.filters.matches",
}

webhooks_settings = AppSettings(
    "STAPEL_WEBHOOKS",
    defaults=DEFAULTS,
    import_strings=("TRANSPORT", "SIGNER", "MATCHER"),
)

__all__ = ["webhooks_settings", "DEFAULTS"]
