# CONFIG.MD — stapel-webhooks settings

Every key below lives in the `STAPEL_WEBHOOKS` dict. Resolution order per
key: `settings.STAPEL_WEBHOOKS` → flat Django setting of the same name →
environment variable → default. The three seam keys (`TRANSPORT`, `SIGNER`,
`MATCHER`) are **never** read from the environment — a name that decides
which code the process loads is not something a stray `export` may choose.

Nothing here needs to be set for the module to boot. What a host actually
has to decide is in §"The four decisions" at the end.

## Delivery registry

| Key | Default | What it does |
|---|---|---|
| `DELIVERY_TYPES` | `{}` | Merged OVER the four built-ins (`webhook`, `notification`, `ws`, `custom`). A spec of `None` **removes** a type — `{"custom": None}` is how a host closes dotted-path delivery. A spec REPLACES, it does not deep-merge. |
| `ALLOWED_CUSTOM_PATHS` | `[]` | Allowlist of dotted paths the `custom` type may name. Empty means no path is callable at all — a path stored in a row is in-process code selected by data. |

## What the reaction layer listens to

| Key | Default | What it does |
|---|---|---|
| `WATCH_CATALOG` | `True` | Derive the watched topics from every installed app's `schemas/emits/*.json`. This is what makes "subscribable" a property of what is installed. |
| `WATCH_EVENTS` | `[]` | Extra topics to watch, for events no installed package ships a schema for. |
| `IGNORE_EVENTS` | this module's own three | Topics the layer refuses to react to. The default keeps it from reacting to its own delivery facts, which is a loop bounded only by the retry ladder. |
| `EXTRA_CATALOG_PATHS` | `[]` | Extra package roots scanned for `schemas/emits/` — how an L1 library with no AppConfig contributes events. |

## Dispatch

| Key | Default | What it does |
|---|---|---|
| `DISPATCH_MODE` | `"deferred"` | `"deferred"`: matching writes rows, the drain delivers. `"inline"`: deliver right after commit, in the emitting process (dev/monolith; `webhooks.W004` says so). |
| `DRAIN_BATCH_SIZE` | `100` | Rows one drain pass claims. |
| `CLAIM_LEASE_SECONDS` | `120` | How long a claimed row is invisible to other drains. |
| `DRAIN_SCHEDULE` | `{"minute": "*"}` | Crontab kwargs for the beat entry. |

## Retry ladder and dead letters

| Key | Default | What it does |
|---|---|---|
| `MAX_ATTEMPTS` | `8` | Attempts before the dead letter. A permanently refused delivery (a 4xx that is not 408/425/429) skips the ladder entirely. |
| `BACKOFF_BASE_SECONDS` | `10` | First delay. |
| `BACKOFF_FACTOR` | `3.0` | Multiplier per attempt. |
| `BACKOFF_CAP_SECONDS` | `3600` | Ceiling on one delay. |
| `JITTER_RATIO` | `0.1` | ± spread, so one outage's backlog does not re-dial in the same second. |
| `DISABLE_AFTER_DEAD` | `5` | Consecutive dead letters after which a subscription deactivates itself and emits `webhooks.subscription.disabled`. `0` disables the guard. |

## Outbound HTTP

| Key | Default | What it does |
|---|---|---|
| `TIMEOUT_SECONDS` | `10.0` | Per-socket timeout. |
| `TOTAL_DEADLINE_SECONDS` | `20.0` | Whole-operation deadline. |
| `MAX_RESPONSE_BYTES` | `4096` | How much of a receiver's body is read into `last_error`. |
| `MAX_PAYLOAD_BYTES` | `262144` | Above this a delivery is dead-lettered at planning time. |
| `USER_AGENT` | `"stapel-webhooks/1.0"` | |
| `ALLOWED_TARGET_HOSTS` | `[]` | Exact-host allowlist for webhook targets. Empty = any public https host (the SSRF guard still applies). |
| `ALLOW_INSECURE_TARGETS` | `False` | **Confession switch.** Permits `http://` and targets resolving to private/loopback/link-local addresses (including the cloud metadata endpoint). `webhooks.W003` reports it on every boot. |

## Signature

| Key | Default | What it does |
|---|---|---|
| `SIGNATURE_HEADER` | `"X-Stapel-Signature"` | Header carrying `t=…,v1=…`. |
| `SIGNATURE_SCHEME` | `"v1"` | Signature element name. |
| `SIGNATURE_TOLERANCE_SECONDS` | `300` | Drift a receiver should accept before treating a request as a replay. Publish this to your receivers. |
| `SECRET_BYTES` | `32` | Entropy of a generated subscription secret. |

## Retention

| Key | Default | What it does |
|---|---|---|
| `SUCCEEDED_RETENTION_DAYS` | `7` | Succeeded deliveries are a log. `None` keeps them forever. |
| `DEAD_RETENTION_DAYS` | `90` | Dead letters are evidence. `None` keeps them forever. |
| `PURGE_SCHEDULE` | `{"hour": 4, "minute": 20}` | Crontab kwargs for the purge beat entry. |

## API surface

| Key | Default | What it does |
|---|---|---|
| `MAX_PAGE_SIZE` | `100` | Keyset page cap. |
| `MAX_SUBSCRIPTIONS_PER_OWNER` | `100` | Per-owner cap; `0` disables it. |
| `MAX_FILTER_DEPTH` | `4` | Widest predicate nesting accepted, validated when the rule is written. |

## Seams (dotted paths; environment-closed)

| Key | Default |
|---|---|
| `TRANSPORT` | `stapel_webhooks.transport.SafeHttpsTransport` |
| `SIGNER` | `stapel_webhooks.signing.HmacSha256Signer` |
| `MATCHER` | `stapel_webhooks.filters.matches` |

## The four decisions

Everything else is tuning. These four change what the deployment *is*:

1. **`DISPATCH_MODE`** — does a receiver's latency land on your request
   thread? Keep `"deferred"` and schedule the drain.
2. **`ALLOW_INSECURE_TARGETS`** — is the SSRF guard on? Keep it off outside
   development; reach an in-cluster receiver with `ALLOWED_TARGET_HOSTS`.
3. **`ALLOWED_CUSTOM_PATHS`** / `DELIVERY_TYPES = {"custom": None}` — may a
   subscription row name in-process code? Ships closed.
4. **`WATCH_CATALOG` / `WATCH_EVENTS`** — what may be subscribed to at all.
