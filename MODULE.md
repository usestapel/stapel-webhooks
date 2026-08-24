# MODULE.md — stapel-webhooks

Integration reference for **stapel-webhooks**: what it stores, what it
exposes, what it asks of a host, and which of its switches are decisions
rather than tuning. `README.md` is the introduction; this is the contract.

Design of record: `docs/pending/forms-events-transports.md` §3 in the stapel
workspace ("подписки на изменения сущностей — реакция-слой над comm") and the
`stapel-webhooks` row of `docs/reference/module-roadmap.md`.

---

## 1. What it is

The **reaction layer**. Business facts already travel the fleet as comm
Actions with committed JSON schemas; what was missing was the consumer side —
a declarative *"when X happens, do Y"* available to the app layer and to
external systems. One matcher, four last miles:

```
comm Action (transactional outbox — already there)
   │
   ▼  stapel-webhooks: subscription registry + matcher
   ├─► webhook       HTTP POST, HMAC-signed, retried, dead-lettered
   ├─► notification  email/push/SMS via stapel-notifications
   ├─► ws            ephemeral frame on a realtime stream (comm Signal)
   └─► custom        allowlisted dotted path in the app layer
```

An **L2 data-plane module**. Two tables:

| Model | Table | Role |
|---|---|---|
| `Subscription` | `webhooks_subscription` | the RULE: event + filter + target + delivery type |
| `Delivery` | `webhooks_delivery` | the EVIDENCE: one attempt-bearing row per (event, subscription) |

App label `webhooks`. UUID primary keys. Both models are
`@access.sensitive` — a subscription carries a signing secret and a
destination (together, everything needed to forge this deployment's
webhooks); a delivery carries a snapshot of the emitting module's payload.

---

## 2. Mounting

```python
INSTALLED_APPS = [..., "stapel_webhooks"]

# urls.py — the module bakes in the api/v1 segment (api-versioning.md §2)
path("webhooks/", include("stapel_webhooks.urls"))     # -> /webhooks/api/v1/...
```

Nothing else is required to boot. What a host adds to make it *deliver* is
the drain (§8).

---

## 3. What can be subscribed to — `event_catalog()`

The vocabulary is **generated, not maintained**:

```
watched = every installed app's schemas/emits/*.json   (WATCH_CATALOG)
        + STAPEL_WEBHOOKS["WATCH_EVENTS"]
        - STAPEL_WEBHOOKS["IGNORE_EVENTS"]
```

Install stapel-moderation and `moderation.report.received` becomes
selectable; uninstall it and the event stops being offered — with no release
of this module either way. The scan reads files only, which is what makes it
legal at `AppConfig.ready()` time (house law §49: no database at boot).

Three ways to ask:

```python
from stapel_webhooks import event_catalog
event_catalog()["listing.published"]["properties"]     # in-process

call("webhooks.event_catalog", {})                     # over comm
GET /webhooks/api/v1/event-catalog                     # over HTTP
python manage.py webhooks_event_catalog                # from a shell
```

An L1 library with no AppConfig (and therefore no app path) contributes its
events through `EXTRA_CATALOG_PATHS`.

---

## 4. HTTP surface

`HasWorkspaceMandateIfScoped` on every route — the library-shaped mandate
gate (`views.py`). An anonymous session is refused in every deployment shape;
beyond that the gate adapts: in a host that can answer the mandate question
it enforces the third principal state (a registered account belonging to no
workspace is a guest, not a user), and in a single-tenant host, where no
mandate exists for anybody to hold, it admits. The strict `HasWorkspaceMandate`
would 503 everyone in the second shape, and the reaction layer must be
installable there. A seam that IS wired and then fails to answer still
raises 503 `error.503.mandate_unavailable` — **a client must handle it**;
unreachable-by-configuration and unreachable-right-now are different facts.

**A caller sees the rules whose `owner_id` is their user id; staff see all.**
There is no per-workspace capability call on top of that, and that is a
choice for the same reason. `workspace_id` is carried on the row for hosts
that do have tenancy, and scoping by it is one subclassed view away (§9).

| Route | Method | Notes |
|---|---|---|
| `/event-catalog` | GET | what may be subscribed to, and the live delivery types |
| `/subscriptions` | GET | the caller's rules (`?event_type=`, `?is_active=`, `?limit=`) |
| `/subscriptions` | POST | **201 returns the signing secret — the only time it is readable** |
| `/subscriptions/<uuid>` | GET, PATCH, DELETE | PATCH re-validates the WHOLE rule |
| `/subscriptions/<uuid>/secret` | POST | rotate; returns the new secret |
| `/subscriptions/<uuid>/deliveries` | GET | the delivery log, incl. dead letters (`?status=`) |
| `/deliveries/<uuid>` | GET | one record, payload included — what a replay would send |
| `/deliveries/<uuid>/replay` | POST | dead letter → queued again, from attempt zero |
| `/error-keys/` | GET | the listing the stapel-translate collector reads |

A stranger's subscription answers **404, not 403**: the id is not public, and
"exists but not yours" is an enumeration oracle for other tenants' rule ids.

### The rule, on the wire

```json
POST /webhooks/api/v1/subscriptions
{
  "event_type": "listing.published",
  "delivery": "webhook",
  "target": {"url": "https://crm.example.com/hooks/stapel"},
  "filter": {"city": "berlin", "price": {"$gte": 500}},
  "description": "CRM sync"
}
→ 201 {"id": "3fa8…", "secret": "whsec_…"}
```

---

## 5. What a receiver sees

```
POST /hooks/stapel HTTP/1.1
Content-Type: application/json
User-Agent: stapel-webhooks/1.0
X-Stapel-Delivery: 9aa1c0de-0000-4000-8000-000000000001
X-Stapel-Event: listing.published
X-Stapel-Event-Id: b0c1…
X-Stapel-Attempt: 2
X-Stapel-Signature: t=1755993600,v1=6f1e…c3

{"id":"9aa1…","type":"listing.published","event_id":"b0c1…",
 "created_at":"2026-08-24T10:00:00+00:00","subscription_id":"3fa8…",
 "data":{ … the emitting module's payload, verbatim … }}
```

**Verification**, for a receiver in any language:

1. read `t` and `v1` from the signature header;
2. refuse if `|now - t|` exceeds your tolerance (ours defaults to 300 s) —
   this is the replay guard, and it works because `t` is inside the signed
   string, so an attacker cannot move it without breaking `v1`;
3. compute `HMAC-SHA256(secret, f"{t}.{raw_body}")`, hex;
4. compare in constant time **against the raw bytes you received** — never
   against a re-serialized object; key order and spacing will bite.

A receiver inside the fleet skips all of that:
`stapel_webhooks.verify(secret, body, header)`. Rotation is supported —
`verify` accepts a list of secrets, so old and new both pass for the length
of the overlap.

**De-duplication is the receiver's job and we make it possible**:
`X-Stapel-Delivery` is identical across every retry of the same fact,
because it *is* the delivery row's identity.

---

## 6. The delivery state machine

```
                 ┌───────────► succeeded   (2xx)
   pending ──────┤
      ▲          └──► retrying ──► … ──► dead   (ladder exhausted)
      │                  │
   replay ◄──────────────┴──────────────── dead   (4xx that is not 408/425/429,
                                                   blocked address, bad scheme,
                                                   oversized payload, unknown
                                                   delivery type)
```

* **retryable vs not** is the one judgement that matters. A receiver that is
  down earns the ladder; a receiver that answered 400 does not, and eight
  identical refusals teach nobody anything.
* **backoff** = `min(BASE * FACTOR**(n-1), CAP)` ± `JITTER_RATIO`. With the
  defaults the ladder spans ~2 h over 8 attempts — the outage a receiver can
  plausibly have — and the jitter is what stops one outage's whole backlog
  from re-dialling in the same second.
* **dead-letter is a state, not a deletion.** The payload stays; `/replay`
  puts it back with the full ladder, because a replay is a decision by
  someone who has presumably fixed something.
* **a rule that keeps dying disables itself** after `DISABLE_AFTER_DEAD`
  consecutive dead letters, and emits `webhooks.subscription.disabled`. Any
  success resets the count; re-activating clears it.
* **at-least-once is absorbed, not fought.** `Delivery.idempotency_key` is
  `<event_id>:<subscription_id>` and is unique, so a redelivered Action plans
  nothing.

---

## 7. Comm surface

**Emits** (schemas in `schemas/emits/`) — ids and outcomes only, never the
delivered payload, and never a secret:

| Event | When |
|---|---|
| `webhooks.delivery.succeeded` | a reaction reached its subscriber |
| `webhooks.delivery.dead` | the ladder is exhausted, or a permanent refusal |
| `webhooks.subscription.disabled` | a rule deactivated itself |

All three are in `IGNORE_EVENTS` by default: a subscription on
`webhooks.delivery.dead` whose own delivery dies emits another one, and the
loop is bounded only by the retry ladder.

**Functions** (schemas in `schemas/functions/`):

| Function | Answers |
|---|---|
| `webhooks.event_catalog` | `{"events": [...]}` — the subscribable vocabulary |
| `webhooks.dispatch` | `{"planned": n}` — hand in a fact from outside this deployment's Action bus (idempotent on `event_id`) |

**Consumes**: every watched topic (§3), dynamically. The handler never raises
into the bus — a reaction-layer failure must not fail the Action for the
other subscribers of that topic.

---

## 8. What a host must actually do

1. **Schedule the drain.** With `DISPATCH_MODE = "deferred"` (the default)
   nothing is delivered until it runs:

   ```python
   from stapel_webhooks.tasks import get_webhooks_beat_schedule
   CELERY_BEAT_SCHEDULE = {**get_webhooks_beat_schedule(), ...}
   ```

   or from cron: `manage.py deliver_webhooks` (add `--loop` for a worker).
   `webhooks.W002` reports a beat schedule with no drain entry.
2. **Schedule the purge** (`manage.py purge_webhook_deliveries`), or accept
   that succeeded deliveries accumulate.
3. **Decide about `custom`.** It ships closed: `ALLOWED_CUSTOM_PATHS` is
   empty, so no dotted path is callable. Either list your handlers, or
   remove the type entirely with `DELIVERY_TYPES = {"custom": None}`.
4. **Leave the SSRF guard on.** `ALLOW_INSECURE_TARGETS` permits `http://`
   and private/loopback/link-local targets, the cloud metadata endpoint
   included. Use `ALLOWED_TARGET_HOSTS` for a known in-cluster receiver
   instead. `webhooks.W003` reports it on every boot.

Full settings table: `CONFIG.MD`.

---

## 9. Extension points (the agent-facing map)

| Seam | How | What it changes |
|---|---|---|
| `STAPEL_WEBHOOKS["DELIVERY_TYPES"]` | merge-registry: built-ins ← settings ← `register_delivery_type()`; `None` removes | adds/replaces/removes a delivery kind. A spec REPLACES (no deep merge) |
| `registry.register_delivery_type(name, spec)` | at runtime | same, for a host that registers after boot |
| `STAPEL_WEBHOOKS["TRANSPORT"]` | dotted path → object with `post(url, body, headers)` + `classify(status)` | egress proxy, mTLS, a test double. The default carries the SSRF guard |
| `STAPEL_WEBHOOKS["SIGNER"]` | dotted path → object with `sign()` / `verify()` | speak another provider's signature dialect |
| `STAPEL_WEBHOOKS["MATCHER"]` | dotted path → `callable(payload, predicate) -> bool` | a different filter language |
| `spec["target_validator"]` | dotted path → `callable(target)` raising `InvalidTarget` | per-type target rules (`custom` uses it for the path allowlist) |
| `STAPEL_SWAP["WEBHOOKS_SUBSCRIPTION_PRESENTER"]` / `…_DELIVERY_PRESENTER` | dotted path → `Presenter` subclass | reshape either response body |
| `views.SerializerSeamMixin` | subclass a view, set `request_serializer_class` / `response_serializer_class` | swap either serializer without rewriting a method |
| `STAPEL_WEBHOOKS["EXTRA_CATALOG_PATHS"]` | list of package roots | let a non-app library contribute events |

A delivery-type spec:

```python
{
  "handler": "myapp.deliveries.to_slack",      # callable(DeliveryContext) -> DeliveryResult
  "required_target_keys": ("channel",),
  "any_of_target_keys": (),                    # at least one, when non-empty
  "target_validator": "myapp.deliveries.check", # optional
  "signed": False,                             # does it carry an HMAC secret
  "external": True,                            # does it leave the deployment
  "description": "Slack",
}
```

`DeliveryResult(ok, retryable, status_code, detail)`. `retryable` is only
consulted when `ok` is false, and it is the whole contract: it decides
between the ladder and the dead letter.

---

## 10. The filter language

A JSON predicate over the event payload. Keys AND together; `$or`, `$and`,
`$not` group; paths are dotted and traverse objects only.

| | |
|---|---|
| equality | `{"city": "berlin"}` |
| operators | `$eq $ne $in $nin $gt $gte $lt $lte $exists $contains $prefix` |
| grouping | `{"$or": [...]}`, `{"$and": [...]}`, `{"$not": {...}}` |

Deliberate non-features, each a refusal rather than an omission:

* **no regular expressions.** A predicate is authored by whoever owns the
  subscription and evaluated in our process on every matching event; a regex
  there is a catastrophic-backtracking lever pointed at the dispatcher.
* **no expressions, no eval.** Every operator is a fixed name.
* **bounded depth** (`MAX_FILTER_DEPTH`), validated when the rule is written,
  so a hostile predicate is never paid for at delivery time.
* **missing is not null.** `{"city": null}` matches a payload whose `city`
  IS null; `{"city": {"$exists": false}}` is how "lacks the key" is spelled.
  Conflating them is how a filter silently widens.

---

## 11. Known limitations, stated rather than papered over

* **The POST guard is a near-copy, not a reuse.** `stapel_core.net.safe_fetch`
  is GET-only and takes no body or headers, so `transport.py` re-states the
  POST shape around core's `ip_is_forbidden` (the IP *policy* — the part that
  must never drift — is imported, not copied). A `post_bytes` in
  `stapel_core.net` is the right home for it and is the follow-up.
* **No `capabilities.json` yet.** Since 0.1.1 the module emits its contract
  triad — `docs/{schema,flows,errors}.json`, `make contract`, drift-gated by
  `make contract-check` and `tests/test_contract_triad.py` — from a
  single-module `{webhooks + core}` instance at the canonical
  `/webhooks/api/v1` prefix (`_codegen.py` / `_codegen_settings.py` /
  `codegen_urls.py`), and all three ship in the wheel. The fourth artifact,
  `docs/capabilities.json` (plus `llms.txt` / an assembled README), is still
  missing, so `stapel-catalog --from-installed` reads the OpenAPI slice but
  no curated capability map. `docs/flows.json` is `[]` on purpose: the
  subscription builder's flow belongs to the client.
* **No GDPR provider.** A subscription's `owner_id` is a user id and its
  target may be a personal email; there is no `user.deleted` consumer yet, so
  a deleted account's rules survive it. Deliberate for 0.1.0 — the erasure
  question ("delete the rule, or orphan it so an operator can see what was
  still being delivered?") is a product decision, not a mechanical one — and
  it is the first thing 0.2.0 should close.
* **Ownership is user-scoped, not capability-scoped** (§4). Multi-tenant
  hosts that need `workspace_id` scoping subclass the views today.
* **No admin surface for the dead-letter queue beyond the peephole.** The
  Django admin is read-only by design; bulk replay is a management command
  away and is not written yet.
