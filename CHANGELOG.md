# Changelog

All notable changes to stapel-webhooks are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Pre-1.0 semver: **minor = breaking**, patch = compatible.

## [Unreleased]

## [0.1.1] — 2026-08-24

### Added
- **Own contract triad** (`_codegen.py` / `codegen_urls.py`, `make contract`):
  `docs/schema.json`, `docs/flows.json` and `docs/errors.json` are now build
  artifacts of this repo, emitted from a single-module `{webhooks + core}`
  Django instance mounted at the canonical `/webhooks/api/v1` prefix
  (contract-pipeline.md §2-3). The module is in no host's aggregate, so there
  was no OpenAPI for it anywhere — a frontend codegen could not generate a
  client and the react pair would have hand-written its types off
  `presenters.py` (BACKEND-GAP X-1/W-1). `_codegen_settings.py` grew a
  `contract=True` mode carrying the production `REST_FRAMEWORK` block, so the
  emitted schema is the one a real deployment serves; the test suite keeps
  using the same settings function, so the two cannot drift.
  `make contract-check` is the drift gate (also wired into `make check`), and
  `tests/test_contract_triad.py` is the authoritative one: determinism,
  `$ref` closure, canonical prefix, the documented ten-operation surface, and
  the promise that no operation is anonymous.
- `docs/{schema,flows,errors}.json` added to `package-data` — the contract
  ships in the wheel, so `stapel-catalog --from-installed` reads it off the
  lockfile.
- CI installs `stapel-tools` for the test job (the emitters the drift gate
  drives).

### Fixed
- **MODULE.md §4 named the wrong gate.** It said `IsNotAnonymousUser` on every
  route; the code has always used `HasWorkspaceMandateIfScoped` (`views.py`).
  The difference is not cosmetic — the real gate enforces the guest state in a
  multi-tenant host and can answer **503 `error.503.mandate_unavailable`**,
  which a client written against the documented gate would never handle
  (BACKEND-GAP W-9). No behaviour change; the document now describes the code.
- MODULE.md §11 no longer claims the module emits no contract triad.

## [0.1.0] — 2026-08-24

First release. The reaction layer of
`docs/pending/forms-events-transports.md` §3: a subscription registry over
comm Actions, with four deliveries of one mechanism.

### Added

- **`Subscription`** — the user-facing reaction rule (`event_type` +
  `filter` + `target` + delivery type), and **`Delivery`** — one
  attempt-bearing row per (event, subscription) pair, with a unique
  `<event_id>:<subscription_id>` idempotency key that absorbs at-least-once
  Action redelivery.

- **The delivery merge-registry** (`registry.py`) with the fleet's
  semantics: built-ins ← `STAPEL_WEBHOOKS["DELIVERY_TYPES"]` ←
  `register_delivery_type()`, a spec of `None` removing a type. Four
  built-ins: `webhook`, `notification`, `ws`, `custom`.

- **Webhook delivery**: HMAC-SHA256 signature in the Stripe-shaped
  `t=…,v1=…` header over the exact bytes sent, exponential backoff with a
  cap and jitter, a dead-letter state that keeps the payload, replay from
  the API, and a subscription that deactivates itself after consecutive
  dead letters. `verify()` is exported so a receiver inside the fleet
  verifies with the code that signed, and it accepts a list of secrets so a
  rotation has an overlap.

- **`event_catalog()`** — the subscribable vocabulary, generated from every
  installed app's `schemas/emits/*.json` (plus `EXTRA_CATALOG_PATHS` for
  L1 libraries with no AppConfig). Reachable in-process, over comm
  (`webhooks.event_catalog`), over HTTP (`/event-catalog`) and from a shell
  (`manage.py webhooks_event_catalog`). No database is touched building it,
  which is what makes the Action wiring legal at `ready()` (house law §49).

- **Dynamic Action subscriptions**: one handler per watched topic, derived
  from the catalog plus `WATCH_EVENTS` minus `IGNORE_EVENTS`, wired at
  `ready()` and idempotent on re-entry. The handler never raises into the
  bus — a reaction-layer failure must not fail the Action for the other
  subscribers of that topic.

- **A bounded filter language** (`filters.py`): dotted paths, eleven
  operators, `$or`/`$and`/`$not`, validated when the rule is written.
  Deliberately without regular expressions (a catastrophic-backtracking
  lever pointed at the dispatcher), and with `missing` distinguished from
  `null`.

- **REST surface** under `/webhooks/api/v1/`: subscription CRUD, secret
  rotation, per-rule delivery log, delivery detail and replay, the event
  catalog, and the `error-keys/` listing the stapel-translate collector
  reads. Owner-scoped; a stranger's rule answers 404 rather than 403, so the
  endpoint is not an id oracle.

- **Five system checks**, each describing a deployment that boots cleanly
  and delivers nothing: an empty watch set (W001), deferred dispatch with no
  drain scheduled (W002), the SSRF guard switched off (W003), inline
  dispatch on the request thread (W004), and live rules naming an
  uninstalled event or an unregistered delivery type (W005/W006).

- **Three emitted facts** (`webhooks.delivery.succeeded` / `.dead`,
  `webhooks.subscription.disabled`), ids and outcomes only — the delivered
  payload does not ride the bus a second time, and no secret ever does. All
  three are ignored by the layer itself by default, because a dead letter
  about a dead letter is a loop bounded only by the retry ladder.

- Management commands `deliver_webhooks`, `purge_webhook_deliveries`,
  `webhooks_event_catalog`; celery-optional beat entries via
  `get_webhooks_beat_schedule()`; a read-only admin peephole that never
  displays a secret; ru/es error catalogues.

### Security posture

Every switch that trades safety for reach ships **closed**, and each one that
a host may legitimately open prints a check when opened:

- webhook targets must be https and must resolve to a public address; the
  IP policy is `stapel_core.net.safe_fetch.ip_is_forbidden` (private,
  loopback, link-local incl. `169.254.169.254`, CGNAT, and the IPv4-mapped /
  6to4 / NAT64 re-encodings of all of them), the connection is pinned to the
  validated IP with SNI for the real hostname, and **redirects are never
  followed** — a signed POST must not be replayed at an address the
  subscription's owner never named;
- `DISPATCH_MODE` defaults to `"deferred"`: a stranger's endpoint is not on
  the request thread;
- the `custom` delivery type is unusable until `ALLOWED_CUSTOM_PATHS` names
  handlers, re-checked at delivery time as well as at authoring time,
  because the row outlives the setting;
- the signing secret is returned exactly twice in a subscription's life (on
  create, on rotate) and is absent from every read, from the admin, and from
  every emitted event.

### Known gaps (see MODULE.md §11)

No GDPR provider yet (a deleted account's rules survive it); no contract
triad emission (`docs/*.json`); ownership is user-scoped rather than
workspace-capability-scoped; the POST-shaped SSRF guard is a near-copy of
core's GET-only `safe_fetch` and wants a `post_bytes` upstream.
