# stapel-webhooks

**The reaction layer.** Your modules already emit facts — `user.registered`,
`listing.published`, `payment.completed` — as schema-typed comm Actions
riding a transactional outbox. This is the consumer side: a declarative
*"when X happens, deliver it to Y"* that the app layer and external systems
can both use.

```
comm Action (already there)
   │
   ▼  subscription registry + matcher
   ├─► webhook       HTTP POST, HMAC-signed, retried, dead-lettered
   ├─► notification  email/push/SMS via stapel-notifications
   ├─► ws            live frame on a realtime stream
   └─► custom        allowlisted dotted path in your app
```

Part of the [Stapel](https://github.com/usestapel) framework.

## Install

```bash
pip install stapel-webhooks
```

```python
INSTALLED_APPS = [..., "stapel_webhooks"]

path("webhooks/", include("stapel_webhooks.urls"))   # -> /webhooks/api/v1/...
```

Then schedule the drain — with the default deferred dispatch, nothing is
delivered until it runs:

```python
from stapel_webhooks.tasks import get_webhooks_beat_schedule
CELERY_BEAT_SCHEDULE = {**get_webhooks_beat_schedule(), ...}
```

…or from cron: `manage.py deliver_webhooks`. (A system check tells you if you
forget.)

## Subscribe to something

```json
POST /webhooks/api/v1/subscriptions
{
  "event_type": "listing.published",
  "delivery": "webhook",
  "target": {"url": "https://crm.example.com/hooks/stapel"},
  "filter": {"city": "berlin", "price": {"$gte": 500}}
}
→ 201 {"id": "3fa8…", "secret": "whsec_…"}
```

The secret is returned once, on creation (and again only on rotation) — like
an API key.

## What is subscribable

Not a list somebody maintains: **every event any installed module ships a
schema for**, scanned out of `schemas/emits/`.

```bash
python manage.py webhooks_event_catalog
```

Install a module and its facts become subscribable. Uninstall it and they
stop being offered. No release of this package either way.

## What your receiver gets

```
X-Stapel-Delivery: 9aa1c0de-…      ← identical across retries; de-duplicate on it
X-Stapel-Event: listing.published
X-Stapel-Signature: t=1755993600,v1=6f1e…c3

{"id":"9aa1…","type":"listing.published","created_at":"…","data":{ … }}
```

Verify: `HMAC-SHA256(secret, f"{t}.{raw_body}")`, compared in constant time
against the **raw bytes** — and refuse a `t` outside your tolerance window,
which is the replay guard. In Python, `stapel_webhooks.verify(secret, body,
header)` does it for you and accepts a list of secrets so rotation has an
overlap.

## When the receiver is down

Exponential backoff with a cap and jitter (8 attempts over ~2 h by default),
then a **dead letter** — a row that keeps the payload and can be replayed
from the API with the full ladder again. A 4xx that is not 408/425/429 skips
the ladder entirely: eight identical refusals teach nobody anything. A rule
that keeps dying deactivates itself and says so on the bus.

## Closed by default

* webhook targets must be **https** and must resolve to a **public** address
  — private, loopback, link-local and the cloud metadata endpoint are
  refused, with no second DNS lookup between check and connect;
* delivery does **not** happen on your request thread;
* the `custom` delivery type — a dotted path named by a database row — is
  **unusable** until you allowlist your handlers, and can be removed
  outright.

Each of those has a system check that reports it if you open it.

## Documentation

* `MODULE.md` — the integration contract: HTTP surface, comm surface, the
  delivery state machine, every extension point, and the known limitations.
* `CONFIG.MD` — every setting, and the four that are decisions rather than
  tuning.

## License

MIT
