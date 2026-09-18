"""Every response body the contract declares is a body the views actually send.

``docs/schema.json`` is emitted from the views' ``@extend_schema``
annotations, and an annotation is a CLAIM: it says what the view returns, and
the generator has no way to check it against the method body.
``tests/test_contract.py`` compares the committed document against a FRESH
EMISSION of the same annotations — it proves the file is not stale, and
nothing else, because both sides come from the claim. stapel-alerts 0.2.0
shipped ``GET /issues`` declared as ``Issue[]`` while the wire carried
``{count, offset, limit, results}``: the drift gate was green and the
frontend pair rendered ``undefined``.

This is the gate the generator cannot be: it performs every operation the
committed schema declares with a JSON response body, and validates the body
it gets against the schema it was promised.

Rules this file holds itself to:

* an operation with a declared JSON response and no entry in ``RECIPES``
  FAILS LOUDLY — a gate that quietly covers three of four rows is the family
  of green that proves nothing;
* a path parameter the gate cannot fill fails at the point of substitution,
  naming the operation;
* the operations that genuinely cannot be driven in-process are listed by
  name in ``UNDRIVABLE`` with a one-line reason each. That list is asserted
  to be exactly current: a stale entry, or a missing reason, fails;
* a collection that comes back empty fails in the populated pass — an empty
  array validates against any item schema, so an empty answer is a check
  that looked at nothing. That covers the nested collections too
  (``events``, ``delivery_types``), which a generic "is the body a list"
  check cannot see;
* every read is driven a SECOND time in its emptiest legal state
  (``EMPTY_STATE``): a subscription with deliveries and one with none, a
  catalog with events and one with none, a rule carrying none of its
  optional values. Every null finding in the first wave of this gate was
  there.

Runs on every interpreter: it reads the committed schema and never emits.

THE MOUNT. ``codegen_urls.py`` mounts ``webhooks/`` → ``stapel_webhooks.urls``,
which contributes ``api/v1/``. ``tests/urls.py`` mounts the identical prefix,
so this module's suite has always been looking where the document points —
unlike five of the first eight libraries in this wave, whose test urlconf
pointed somewhere the document does not describe. The emission mount is
declared here rather than borrowed, so
``test_every_declared_path_resolves_under_this_urlconf`` fails at the one
moment it is cheap to fix: when somebody changes a mount.

What it found on its first run — 9 of 9 operations driven, 2 red, both the
same defect:

* ``GET /deliveries/{delivery_id}`` and ``GET
  /subscriptions/{subscription_id}/deliveries`` declare
  ``DeliveryPresenterDTO`` with ``response_status`` as a REQUIRED,
  non-nullable ``integer``. The column is ``models.IntegerField(null=True)``
  (``models.py``, ``Delivery.response_status``) and is null for every
  delivery that has not been attempted yet — which is every row the instant
  it is planned (``services.plan_delivery``), and every row again after a
  replay (``services.replay`` sets ``response_status = None`` explicitly).
  A delivery log is read exactly when something is pending or was just
  requeued, so this is the ordinary state, not a corner.

  Root cause is one level up, in stapel-core: ``DeliveryPresenter.fields``
  lists ``response_status`` as an as-is model field, and
  ``stapel_core.django.api.presenters._infer_type`` (presenters.py:130) maps
  a Django field class to a Python type through ``_TYPE_MAP`` without ever
  looking at ``field.null`` — so ``IntegerField(null=True)`` infers ``int``,
  the dataclass field is non-optional, and the emitter copies that into
  ``required`` with no ``nullable``. Every presenter in the fleet that names
  a nullable column in ``fields`` carries the same claim.

stapel-core 0.74.0 teaches ``_infer_type`` to read ``field.null``, so the
presenter declares ``response_status`` nullable and both operations are
honest. The floor names that core, the document is emitted against it, and
``KNOWN_MISMATCHES`` is empty. ``test_the_gate_is_not_blind`` proves that is a
finding rather than a gate that never looked.
"""
import copy
import json
import re
import uuid
from pathlib import Path

import jsonschema
import pytest
from django.test import override_settings
from django.urls import include, path as url_path
from rest_framework.test import APIClient

REPO = Path(__file__).resolve().parent.parent
SCHEMA = json.loads((REPO / "docs" / "schema.json").read_text())

#: The mount the contract is emitted at, reproduced for the test client
#: (``codegen_urls.py``: ``webhooks/`` → ``stapel_webhooks.urls``, which
#: contributes ``api/v1/``).
urlpatterns = [
    url_path("webhooks/", include("stapel_webhooks.urls")),
]

pytestmark = [pytest.mark.django_db, pytest.mark.urls(__name__)]

V1 = "/webhooks/api/v1"

#: A topic a rule may name. The real vocabulary comes from installed
#: packages' ``schemas/emits/``; only stapel-webhooks' own are installed
#: here, so the gate declares what it emits through the same door a host
#: would use.
WATCHED = "listing.published"


class RecordingTransport:
    """Records instead of dialling, and answers a scripted status.

    The outbound half of a webhook is the one thing that cannot run
    in-process. Everything this gate is about — the row the attempt writes,
    the presenter that renders it — runs for real.
    """

    def __init__(self, status=200):
        self.status = status
        self.calls = []

    def post(self, url, body, headers=None, **kwargs):
        from stapel_webhooks.transport import TransportResponse

        self.calls.append(url)
        return TransportResponse(status=self.status, body="", headers={})

    def classify(self, status):
        from stapel_webhooks.transport import SafeHttpsTransport

        return SafeHttpsTransport().classify(status)


def webhooks_settings(**extra):
    """A fresh override each time: one instance cannot be entered twice."""
    config = {
        "WATCH_EVENTS": [WATCHED],
        "TRANSPORT": RecordingTransport(),
    }
    config.update(extra)
    return override_settings(STAPEL_WEBHOOKS=config)


@pytest.fixture(autouse=True)
def _watched_and_transport():
    """The two seams every recipe needs, for the whole test."""
    with webhooks_settings():
        yield


@pytest.fixture(autouse=True)
def _clean_registries():
    """Delivery types, runtime topics and the catalog cache are
    process-global by design — reset them so one recipe never leaks into
    the next (the suite's own conftest does this for its tests)."""
    from stapel_webhooks import actions, catalog, registry

    catalog.reset_catalog()
    yield
    registry.reset_delivery_types()
    actions.reset_runtime_events()
    catalog.reset_catalog()


@pytest.fixture(autouse=True)
def _media_root(tmp_path):
    """Nothing here writes files today; pin the root so nothing ever does.

    ``MEDIA_ROOT`` is unset in this module's harness settings, so it defaults
    to the working directory — in stapel-auth that put a data export into the
    checkout, where under a flat package layout a stray directory also
    shadowed a real submodule.
    """
    with override_settings(MEDIA_ROOT=str(tmp_path)):
        yield


# ─────────────────────────────────────────────────────────────────────────────
# The contract side: what the document declares
# ─────────────────────────────────────────────────────────────────────────────


def _json_schema(node):
    """OpenAPI 3.0 → JSON Schema, for the divergence that matters here.

    OAS 3.0 spells "may be null" as ``nullable: true`` beside a ``type``;
    JSON Schema has no such keyword and would refuse the null — which is
    what half the subscription shape answers the moment a rule has not
    fired yet. Everything else drf-spectacular emits here (``$ref``,
    ``required``, ``additionalProperties``) is JSON Schema as written.
    """
    if isinstance(node, list):
        return [_json_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    rebuilt = {k: _json_schema(v) for k, v in node.items() if k != "nullable"}
    if node.get("nullable"):
        return {"anyOf": [rebuilt, {"type": "null"}]}
    return rebuilt


def _validator(response_schema):
    root = copy.deepcopy(response_schema)
    root["components"] = copy.deepcopy(SCHEMA["components"])
    return jsonschema.Draft202012Validator(_json_schema(root))


def _operations():
    """Every ``(method, path, 2xx code, JSON body schema)`` the contract declares."""
    ops = []
    for path, methods in SCHEMA["paths"].items():
        for method, op in methods.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            for code, response in op.get("responses", {}).items():
                body = (
                    response.get("content", {})
                    .get("application/json", {})
                    .get("schema")
                )
                if body is not None and code.startswith("2"):
                    ops.append((method.upper(), path, int(code), body))
    return sorted(ops, key=lambda o: (o[1], o[0], o[2]))


OPERATIONS = _operations()


# ─────────────────────────────────────────────────────────────────────────────
# The wire side: harness
# ─────────────────────────────────────────────────────────────────────────────


def _unique(prefix):
    return f"{prefix}{uuid.uuid4().hex[:10]}"


def make_user(**kwargs):
    from django.contrib.auth import get_user_model

    defaults = dict(
        username=_unique("wire_"), email=f"{_unique('wire-')}@example.com"
    )
    defaults.update(kwargs)
    return get_user_model().objects.create(**defaults)


def client_for(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def make_subscription(owner, **overrides):
    from stapel_webhooks import services

    fields = dict(
        event_type=WATCHED,
        delivery="webhook",
        target={"url": "https://example.com/hooks/wire"},
        owner_id=owner.pk,
    )
    fields.update(overrides)
    return services.create_subscription(**fields)


def plan(subscription, event_id=None, payload=None):
    """A delivery row as the dispatcher writes it: pending, never attempted."""
    from stapel_webhooks import services

    delivery, _ = services.plan_delivery(
        subscription,
        event_type=WATCHED,
        event_id=event_id or _unique("evt-"),
        payload=payload if payload is not None else {"id": "1"},
    )
    return delivery


def attempted(subscription, status=200):
    """A delivery that has been tried, so ``response_status`` carries an int."""
    from stapel_webhooks import services

    delivery = plan(subscription)
    with webhooks_settings(TRANSPORT=RecordingTransport(status)):
        return services.attempt(delivery)


def dead(subscription):
    """A dead letter — the only replayable state."""
    from stapel_webhooks.models import STATUS_DEAD, Delivery

    delivery = plan(subscription)
    Delivery.objects.filter(pk=delivery.pk).update(status=STATUS_DEAD, attempts=8)
    delivery.refresh_from_db()
    return delivery


# ─────────────────────────────────────────────────────────────────────────────
# The recipe table
# ─────────────────────────────────────────────────────────────────────────────


class Call:
    """Performs one declared operation, and refuses to guess a path parameter."""

    def __init__(self, method, path):
        self.method = method
        self.path = path

    def __call__(self, client, params=None, data=None, query="", **extra):
        url = self.path
        for name, value in (params or {}).items():
            url = url.replace("{%s}" % name, str(value))
        assert "{" not in url, (
            f"{self.method} {self.path}: a path parameter this gate does not "
            "know how to fill — teach its recipe, or the operation goes unchecked"
        )
        send = getattr(client, self.method.lower())
        if self.method in ("GET", "DELETE"):
            return send(url + query, **extra)
        return send(url + query, data if data is not None else {}, format="json", **extra)


#: How to perform each operation the contract declares with a JSON response
#: body, keyed by ``(METHOD, path template)``. A recipe returns the response it
#: produced, or a list of ``(label, response)`` pairs when one operation has
#: more than one answering state worth asking.
RECIPES = {}

#: The same operations again, in the emptiest state the contract still has to
#: describe: a rule with no deliveries, a catalog with no events, a
#: subscription carrying none of its optional values.
EMPTY_STATE = {}


def recipe(method, path, table=None):
    def register(fn):
        target = RECIPES if table is None else table
        key = (method, V1 + path)
        assert key not in target, f"duplicate recipe for {method} {path}"
        target[key] = fn
        return fn

    return register


def empty_state(method, path):
    return recipe(method, path, table=EMPTY_STATE)


#: Operations that cannot be driven in-process, by name and with the reason.
#: A short, visible list is acceptable here; a silent skip is not.
#:
#: EMPTY. The one thing that genuinely cannot happen in-process — dialling
#: the receiver — is a declared ``TRANSPORT`` seam, so the gate wires it the
#: way a host does and everything on this side of it runs for real.
UNDRIVABLE: dict = {}

#: Collections nested inside an object body that must actually carry a row in
#: the populated pass. An empty array validates against any item schema, so a
#: populated run that leaves one empty looked at nothing.
POPULATED_COLLECTIONS = {
    ("GET", V1 + "/event-catalog"): ("events", "delivery_types"),
}


# ── the event catalog ────────────────────────────────────────────────────────


@recipe("GET", "/event-catalog")
def _catalog(call):
    return call(client_for(make_user()))


@empty_state("GET", "/event-catalog")
def _catalog_empty(call):
    """A deployment that watches nothing: ``events`` is [] and the shape
    still has to be the declared one."""
    with webhooks_settings(WATCH_EVENTS=[], WATCH_CATALOG=False):
        from stapel_webhooks import catalog

        catalog.reset_catalog()
        return call(client_for(make_user()))


# ── subscriptions ────────────────────────────────────────────────────────────


@recipe("GET", "/subscriptions")
def _subscriptions_list(call):
    owner = make_user()
    make_subscription(owner, description="CRM sync", payload_filter={"city": "berlin"})
    make_subscription(owner, delivery="ws", target={"stream": "listings:ws:1"})
    return call(client_for(owner))


@empty_state("GET", "/subscriptions")
def _subscriptions_list_empty(call):
    """A caller who has written no rules — and, because the list is scoped
    by owner, cannot see anybody else's."""
    make_subscription(make_user())
    return call(client_for(make_user()))


@recipe("POST", "/subscriptions")
def _subscription_create(call):
    return call(
        client_for(make_user()),
        data={
            "event_type": WATCHED,
            "delivery": "webhook",
            "target": {"url": "https://example.com/hooks/wire"},
            "filter": {"city": "berlin"},
            "description": "Created by the wire gate",
        },
    )


@empty_state("POST", "/subscriptions")
def _subscription_create_empty(call):
    """An unsigned delivery type: no secret is minted, so ``secret`` is the
    empty string — the declared answer is a string, not null."""
    return call(
        client_for(make_user()),
        data={
            "event_type": WATCHED,
            "delivery": "ws",
            "target": {"stream": "listings:ws:1"},
        },
    )


@recipe("GET", "/subscriptions/{subscription_id}")
def _subscription_get(call):
    owner = make_user()
    row = make_subscription(
        owner, description="CRM sync", payload_filter={"city": "berlin"}
    )
    attempted(row)
    row.refresh_from_db()
    return call(client_for(owner), params={"subscription_id": row.id})


@empty_state("GET", "/subscriptions/{subscription_id}")
def _subscription_get_empty(call):
    """A rule that has never fired: ``last_delivery_at`` and ``disabled_at``
    are null, ``description`` is the empty string and ``filter`` is ``{}``
    — the four fields most likely to be a lie in this shape."""
    owner = make_user()
    row = make_subscription(owner)
    return call(client_for(owner), params={"subscription_id": row.id})


@recipe("PATCH", "/subscriptions/{subscription_id}")
def _subscription_patch(call):
    owner = make_user()
    row = make_subscription(owner)
    return call(
        client_for(owner),
        params={"subscription_id": row.id},
        data={"filter": {"city": "berlin"}, "description": "CRM", "is_active": False},
    )


@empty_state("PATCH", "/subscriptions/{subscription_id}")
def _subscription_patch_empty(call):
    """Clearing both optional values back out: the declared answers are the
    empty string and ``{}``, not null."""
    owner = make_user()
    row = make_subscription(
        owner, description="CRM sync", payload_filter={"city": "berlin"}
    )
    return call(
        client_for(owner),
        params={"subscription_id": row.id},
        data={"filter": {}, "description": ""},
    )


@recipe("POST", "/subscriptions/{subscription_id}/secret")
def _subscription_rotate(call):
    owner = make_user()
    row = make_subscription(owner)
    return call(client_for(owner), params={"subscription_id": row.id})


# ── the delivery log ─────────────────────────────────────────────────────────


@recipe("GET", "/subscriptions/{subscription_id}/deliveries")
def _delivery_list(call):
    """Both states of a row in one log, which is what a log actually holds:
    one delivery that has been attempted and one still pending."""
    owner = make_user()
    row = make_subscription(owner)
    attempted(row)
    plan(row)
    return call(client_for(owner), params={"subscription_id": row.id})


@empty_state("GET", "/subscriptions/{subscription_id}/deliveries")
def _delivery_list_empty(call):
    """Two kinds of empty: a log with no rows at all, and a log whose only
    row is itself empty — planned, never attempted, nothing completed. The
    second is the one that matters: an empty ARRAY validates against any item
    schema, so a collection's emptiest interesting state is one row carrying
    none of its optional values."""
    owner = make_user()
    nothing_yet = make_subscription(owner)
    just_planned = make_subscription(owner)
    plan(just_planned)
    return [
        ("no deliveries at all", call(client_for(owner), params={"subscription_id": nothing_yet.id})),
        ("one delivery, planned and never attempted",
         call(client_for(owner), params={"subscription_id": just_planned.id})),
    ]


@recipe("GET", "/deliveries/{delivery_id}")
def _delivery_get(call):
    owner = make_user()
    row = make_subscription(owner)
    return [
        (
            "attempted (response_status is an int)",
            call(client_for(owner), params={"delivery_id": attempted(row).id}),
        ),
        (
            "planned, never attempted (response_status is null)",
            call(client_for(owner), params={"delivery_id": plan(row).id}),
        ),
    ]


@empty_state("GET", "/deliveries/{delivery_id}")
def _delivery_get_empty(call):
    """A row the instant the dispatcher wrote it: no attempt, no response,
    no completion."""
    owner = make_user()
    row = make_subscription(owner)
    return call(client_for(owner), params={"delivery_id": plan(row).id})


@recipe("POST", "/deliveries/{delivery_id}/replay")
def _delivery_replay(call):
    owner = make_user()
    row = make_subscription(owner)
    return call(client_for(owner), params={"delivery_id": dead(row).id})


# ─────────────────────────────────────────────────────────────────────────────
# The gate
# ─────────────────────────────────────────────────────────────────────────────


#: Operations whose declared body the wire does not send, with the defect and
#: its owner. ``strict=True``: a fixed entry fails until it is deleted, so a
#: finding can be neither forgotten nor quietly kept.
KNOWN_MISMATCHES: dict[tuple[str, str], str] = {}


def test_the_contract_declares_something_to_check():
    assert OPERATIONS, "docs/schema.json declares no JSON responses at all"


def test_every_declared_path_resolves_under_this_urlconf():
    """The suite must be looking where the document describes.

    Five of the first eight libraries this gate was written for had a
    committed contract that nothing had ever driven, because the test urlconf
    mounted somewhere the document does not describe: one mounted a different
    prefix AND one segment short, one mounted the paths bare, one mounted a
    doubled segment, one mounted less than the emission did. In every case
    the operations were "covered" by a file that could not have reached a
    single one of them.

    Webhooks is not one of them — ``tests/urls.py`` and ``codegen_urls.py``
    mount the identical ``webhooks/`` prefix — and this assertion is what
    keeps saying so. A missing recipe already fails loudly; this fails when
    the MOUNT is wrong, which no per-operation check can see, because when
    the mount is wrong every operation is equally and silently unreachable.
    """
    from django.urls import Resolver404, resolve

    # Resolution cares about the SHAPE of a segment, and a urlconf may use
    # several converters — uuid, int, slug. A path counts as reachable if any
    # one shape resolves: the question here is whether the mount exists, not
    # whether a particular id does.
    candidates = (
        "00000000-0000-4000-8000-000000000000",
        "1",
        "a-slug",
    )

    unreachable = []
    for _method, path, _code, _schema in OPERATIONS:
        for value in candidates:
            try:
                resolve(re.sub(r"\{[^}]+\}", value, path))
                break
            except Resolver404:
                continue
        else:
            unreachable.append(path)

    assert not unreachable, (
        "these declared paths do not resolve under this module's urlconf, so "
        "nothing here can be driving them — the mount is wrong, not the "
        "recipes:\n  " + "\n  ".join(sorted(set(unreachable)))
    )


def test_every_declared_operation_is_driven_or_named_undrivable():
    """No operation is covered by silence, and no entry outlives its operation."""
    declared = {(method, path) for method, path, _code, _schema in OPERATIONS}
    covered = set(RECIPES) | set(UNDRIVABLE)

    missing = sorted(declared - covered)
    assert not missing, (
        "operations with a declared JSON response body and no recipe:\n"
        + "\n".join(f"  {m} {p}" for m, p in missing)
    )
    stale = sorted(covered - declared)
    assert not stale, (
        "recipes/exclusions for operations the contract no longer declares:\n"
        + "\n".join(f"  {m} {p}" for m, p in stale)
    )
    both = sorted(set(RECIPES) & set(UNDRIVABLE))
    assert not both, f"driven AND excluded: {both}"
    for key, reason in UNDRIVABLE.items():
        assert reason and reason.strip(), f"{key} is excluded with no reason"

    stale_collections = sorted(set(POPULATED_COLLECTIONS) - declared)
    assert not stale_collections, (
        f"nested-collection expectations for undeclared operations: {stale_collections}"
    )


def test_every_read_is_also_driven_in_its_emptiest_state():
    """A populated answer cannot say what a field holds when there is nothing.

    Every null finding in the first wave of this gate was on the empty state —
    including this module's own, which is invisible to a log that only ever
    holds finished rows.
    """
    reads = {
        (method, path)
        for method, path, _code, _schema in OPERATIONS
        if method == "GET"
    }
    missing = sorted(reads - set(EMPTY_STATE))
    assert not missing, (
        "reads driven only against a populated database — the state where "
        "every null claim in this gate's history was found is unchecked:\n"
        + "\n".join(f"  {m} {p}" for m, p in missing)
    )
    declared = {(m, p) for m, p, _c, _s in OPERATIONS}
    stale = sorted(set(EMPTY_STATE) - declared)
    assert not stale, f"empty-state recipes for undeclared operations: {stale}"


def test_every_known_mismatch_is_still_declared_and_explained():
    """A recorded defect must name a live operation and carry its reason.

    Without this, an operation that is renamed or removed leaves an entry that
    silences nothing and reads like a known problem forever.
    """
    declared = {(method, path) for method, path, _code, _schema in OPERATIONS}
    for key, reason in KNOWN_MISMATCHES.items():
        assert key in declared, (
            f"{key} is recorded as a known mismatch but the contract no longer "
            "declares it — delete the entry"
        )
        assert reason and reason.strip(), f"{key} is recorded with no reason"


def _labelled(result):
    """A recipe answers with one response, or with labelled branches."""
    if isinstance(result, list):
        return result
    return [("", result)]


def _drive(table, method, path, code, body_schema, *, expect_rows):
    perform = table.get((method, path))
    assert perform is not None, (
        f"{method} {path} declares a response body and has no recipe — an "
        "unchecked operation is a schema nobody proves. Teach RECIPES, or "
        "name it in UNDRIVABLE with a reason."
    )

    for label, response in _labelled(perform(Call(method, path))):
        where = f"{method} {path}" + (f" [{label}]" if label else "")
        assert response.status_code == code, (
            f"{where}: expected the declared {code}, got "
            f"{response.status_code}: {response.content[:400]}"
        )

        body = response.json()
        errors = sorted(
            _validator(body_schema).iter_errors(body), key=lambda e: list(e.path)
        )
        assert not errors, (
            f"{where} answers a body the contract does not describe:\n"
            + "\n".join(f"  at {list(e.path) or '<root>'}: {e.message}" for e in errors[:10])
            + f"\n  body: {json.dumps(body)[:600]}"
        )

        # An empty list validates against any item schema, so a collection
        # must actually carry a row for the check to have looked at anything.
        if expect_rows:
            if isinstance(body, list):
                assert body, f"{where}: the declared list came back empty"
            for name in POPULATED_COLLECTIONS.get((method, path), ()):
                assert isinstance(body, dict) and body.get(name), (
                    f"{where}: the declared collection {name!r} came back "
                    "empty, so nothing in it was checked"
                )


@pytest.mark.parametrize(
    "method,path,code,body_schema",
    OPERATIONS,
    ids=[f"{m} {p}" for m, p, _c, _s in OPERATIONS],
)
def test_the_wire_matches_the_declared_response(method, path, code, body_schema, request):
    if (method, path) in UNDRIVABLE:
        pytest.skip(f"excluded by name: {UNDRIVABLE[(method, path)]}")

    if (method, path) in KNOWN_MISMATCHES:
        request.node.add_marker(
            pytest.mark.xfail(
                strict=True,
                reason=f"{method} {path}: {KNOWN_MISMATCHES[(method, path)]}",
            )
        )

    _drive(RECIPES, method, path, code, body_schema, expect_rows=True)


_EMPTY_OPERATIONS = [
    (method, path, code, schema)
    for method, path, code, schema in OPERATIONS
    if (method, path) in EMPTY_STATE
]


@pytest.mark.parametrize(
    "method,path,code,body_schema",
    _EMPTY_OPERATIONS,
    ids=[f"{m} {p}" for m, p, _c, _s in _EMPTY_OPERATIONS],
)
def test_the_wire_matches_the_declared_response_when_there_is_nothing_there(
    method, path, code, body_schema, request
):
    """The same claim, asked in the state where the nulls live."""
    if (method, path) in KNOWN_MISMATCHES:
        request.node.add_marker(
            pytest.mark.xfail(
                strict=True,
                reason=f"{method} {path}: {KNOWN_MISMATCHES[(method, path)]}",
            )
        )

    _drive(EMPTY_STATE, method, path, code, body_schema, expect_rows=False)


def test_the_gate_is_not_blind():
    """A canary: swap a declared schema for one the wire cannot satisfy.

    Everything above can be green for two reasons — the claims are honest, or
    the check never looks at the body. This tells them apart by validating a
    real response against ``{"type": "string"}``: every operation here answers
    an object or an array, so every one of them must fail. If any passes, the
    validation in ``_drive`` is not reaching the received body and this whole
    file proves nothing.
    """
    honest = [
        (method, path, code)
        for method, path, code, _schema in OPERATIONS
        if (method, path) not in KNOWN_MISMATCHES and (method, path) not in UNDRIVABLE
    ]
    assert honest, "nothing left to canary"

    survivors = []
    for method, path, code in honest:
        try:
            _drive(RECIPES, method, path, code, {"type": "string"}, expect_rows=False)
        except AssertionError:
            continue
        survivors.append(f"{method} {path}")
    assert not survivors, (
        "these operations passed validation against {'type': 'string'} — the "
        "gate is not looking at the body it received:\n  " + "\n  ".join(survivors)
    )
