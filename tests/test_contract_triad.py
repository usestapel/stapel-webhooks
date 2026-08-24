"""Per-module contract triad + drift gate (contract-pipeline.md §2-3).

stapel-webhooks emits its own triad — ``docs/schema.json`` (OpenAPI),
``docs/flows.json`` (``[]``: the subscription builder's flow is the client's,
no ``@flow_step`` lives here) and ``docs/errors.json`` — from a single-module
``{webhooks + core}`` Django instance mounted at the canonical
``/webhooks/api/v1`` prefix.

The module is not mounted in stapel-example-monolith, so there is no
aggregate slice to diff against for byte-identity — which is precisely why
the triad has to live in this repo: a module whose only OpenAPI is inside
somebody's host is a module no frontend codegen can generate a client for.
Validation is standalone (contract-pipeline.md §9 fallback): determinism,
self-contained ``$ref`` closure, canonical-prefix paths, the documented
operation set, and the promise that nothing here is anonymous.

Regenerate after any change to a serializer/view/url/error key:

    make contract

then commit docs/{schema,flows,errors}.json.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

_PY = sys.version_info[:2]
if _PY != (3, 12):
    _GOT = f"{_PY[0]}.{_PY[1]}"
    pytest.skip(
        "stapel-webhooks contract tests require Python 3.12 (the CI/monolith "
        f"pin) — running {_GOT}. drf-spectacular renders component descriptions "
        "(Optional[X] vs X | None) differently across Python minor versions, so "
        "drift/identity checks emitted+compared under any other minor produce "
        "false diffs. Skipping on any non-3.12 interpreter.",
        allow_module_level=True,
    )

REPO = Path(__file__).resolve().parent.parent
DOCS = REPO / "docs"
TRIAD = ("schema.json", "flows.json", "errors.json")

#: The whole HTTP surface, exactly as MODULE.md §4 tabulates it. A route
#: appearing or vanishing here is a contract change, not a detail.
OPERATIONS = [
    "DELETE /webhooks/api/v1/subscriptions/{subscription_id}",
    "GET /webhooks/api/v1/deliveries/{delivery_id}",
    "GET /webhooks/api/v1/event-catalog",
    "GET /webhooks/api/v1/subscriptions",
    "GET /webhooks/api/v1/subscriptions/{subscription_id}",
    "GET /webhooks/api/v1/subscriptions/{subscription_id}/deliveries",
    "PATCH /webhooks/api/v1/subscriptions/{subscription_id}",
    "POST /webhooks/api/v1/deliveries/{delivery_id}/replay",
    "POST /webhooks/api/v1/subscriptions",
    "POST /webhooks/api/v1/subscriptions/{subscription_id}/secret",
]


def _emit(out_dir: Path) -> None:
    subprocess.run(
        [sys.executable, "-m", "stapel_webhooks._codegen", "--out", str(out_dir)],
        cwd=str(REPO),
        check=True,
        capture_output=True,
    )


def _operations(schema: dict) -> list[str]:
    return sorted(
        f"{method.upper()} {path}"
        for path, ops in schema["paths"].items()
        for method in ops
        if method in ("get", "post", "put", "patch", "delete")
    )


def _schema() -> dict:
    return json.loads((DOCS / "schema.json").read_text())


def test_triad_is_committed():
    for name in TRIAD:
        assert (DOCS / name).is_file(), f"missing docs/{name} — run `make contract`"


def test_triad_has_no_drift(tmp_path):
    _emit(tmp_path)
    for name in TRIAD:
        assert (DOCS / name).read_bytes() == (tmp_path / name).read_bytes(), (
            f"docs/{name} drifted — run `make contract` and commit docs/{name}"
        )


def test_emission_is_deterministic(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _emit(a)
    _emit(b)
    for name in TRIAD:
        assert (a / name).read_bytes() == (b / name).read_bytes()


def test_paths_carry_the_canonical_prefix():
    schema = _schema()
    assert schema["paths"], "schema has no paths"
    assert all(p.startswith("/webhooks/api/v1/") for p in schema["paths"]), (
        "schema paths are not mounted at the canonical /webhooks/api/v1 prefix"
    )


def test_the_documented_operations_are_the_emitted_ones():
    assert _operations(_schema()) == OPERATIONS


def test_nothing_is_anonymous():
    """A reaction layer has no anonymous verb: creating a rule means naming a
    destination this service will dial, and reading one means reading other
    modules' payloads. Every operation therefore carries JWT security."""
    schema = _schema()
    open_ops = []
    for path, operations in schema["paths"].items():
        for method, op in operations.items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            security = op.get("security") or []
            if not any("JWTCookieAuth" in entry for entry in security):
                open_ops.append(f"{method.upper()} {path}")
    assert open_ops == [], open_ops


def _all_refs(obj) -> set[str]:
    return set(re.findall(r'"#/components/schemas/([^"]+)"', json.dumps(obj)))


def test_schema_refs_are_self_contained():
    schema = _schema()
    comps = schema.get("components", {}).get("schemas", {})
    seen: set[str] = set()
    stack = list(_all_refs(schema["paths"]))
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        if name in comps:
            stack.extend(_all_refs(comps[name]))
    dangling = seen - set(comps)
    assert not dangling, f"dangling $ref(s) with no component definition: {dangling}"


def test_flows_are_empty_no_flow_step_annotations():
    flows = json.loads((DOCS / "flows.json").read_text())
    assert flows == [], (
        "docs/flows.json is non-empty but no @flow_step annotation exists in "
        "stapel_webhooks — investigate before assuming [] is still correct"
    )


def test_errors_json_carries_this_module_keys_with_their_remediation():
    from stapel_webhooks.errors import (
        STAPEL_WEBHOOKS_ERRORS,
        STAPEL_WEBHOOKS_REMEDIATION,
    )

    entries = {e["code"]: e for e in json.loads((DOCS / "errors.json").read_text())}
    for code, english in STAPEL_WEBHOOKS_ERRORS.items():
        assert code in entries, f"{code} missing from docs/errors.json"
        assert entries[code]["en"] == english
        assert entries[code]["owner"] == "stapel_webhooks", (
            f"{code} is attributed to {entries[code]['owner']!r} — this module "
            "owns the key and therefore owes its catalogues"
        )
        assert entries[code]["remediation"] == STAPEL_WEBHOOKS_REMEDIATION[code]


def test_the_mandate_refusal_is_in_the_exported_registry():
    """Every route is gated by HasWorkspaceMandateIfScoped, which answers 503
    when the mandate seam is wired but cannot answer. A client that has never
    seen the key cannot render that refusal, so it must be in the artifact the
    client generates from — even though this module does not own it."""
    codes = {e["code"] for e in json.loads((DOCS / "errors.json").read_text())}
    assert "error.503.mandate_unavailable" in codes
