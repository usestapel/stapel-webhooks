"""stapel-webhooks contract-emission harness (contract-pipeline.md §2-3).

Emits the module's own contract triad into ``docs/`` from a single-module
``{webhooks + core}`` Django instance mounted at the canonical
``/webhooks/api/v1`` prefix (api-versioning.md §2):

  docs/schema.json   drf-spectacular OpenAPI, this module only,
                     /webhooks/api/v1 paths
  docs/flows.json    generate_flow_docs machine artifact ([] — no
                     @flow_step annotation lives here; the subscription
                     builder's flow is the client's, not the server's)
  docs/errors.json   generate_error_keys registry

The *mechanism* is stapel_tools.codegen (shared); this file is the thin
per-module *config* wiring the module's settings + canonical mount.

Usage:
    python -m stapel_webhooks._codegen --out docs        # `make contract`
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _configure() -> None:
    """Configure + boot the single-module Django instance for emission."""
    from django.conf import settings

    if not settings.configured:
        from stapel_webhooks._codegen_settings import settings_kwargs

        settings.configure(
            **settings_kwargs(
                root_urlconf="stapel_webhooks.codegen_urls", contract=True
            )
        )

    import django

    django.setup()

    # drf-spectacular froze its settings singleton at import time — pin the
    # one knob that matters for byte-identity with a monolith aggregate
    # (see _codegen_settings.CODEGEN_SCHEMA_PATH_PREFIX).
    from drf_spectacular.settings import spectacular_settings

    from stapel_webhooks._codegen_settings import CODEGEN_SCHEMA_PATH_PREFIX

    spectacular_settings.SCHEMA_PATH_PREFIX = CODEGEN_SCHEMA_PATH_PREFIX

    # A real all-modules deployment registers drf-spectacular's JWT-cookie
    # security-scheme extension as a side effect of its dev-only Swagger
    # URLs. A single-module harness has no co-mounted sibling to trigger it,
    # so without this every protected endpoint — which here is all of them —
    # would emit without its `security` entry.
    from stapel_core.django.openapi.swagger import _register_jwt_auth_extension

    _register_jwt_auth_extension()


def _require_python_312() -> None:
    """Abort emission if not running the pinned 3.12 interpreter.

    drf-spectacular renders component descriptions (``Optional[X]`` vs
    ``X | None``) differently across Python minor versions, so emitting on
    anything but the CI/monolith pin produces false diffs against the
    committed docs/*.json.
    """
    if sys.version_info[:2] != (3, 12):
        got = f"{sys.version_info.major}.{sys.version_info.minor}"
        raise SystemExit(
            f"stapel-webhooks contract emission ABORTED: running Python {got}, "
            "but contracts must be emitted on Python 3.12 (the CI/monolith "
            "pin). Re-run under a 3.12 interpreter."
        )


def main(argv: list[str] | None = None) -> int:
    _require_python_312()

    parser = argparse.ArgumentParser(
        prog="stapel-webhooks-contract",
        description="Emit this module's contract triad (schema.json + flows.json "
        "+ errors.json) into --out, canonical /webhooks/api/v1 prefix.",
    )
    parser.add_argument(
        "--out",
        default="docs",
        help="Output directory for the triad (default: docs).",
    )
    args = parser.parse_args(argv)

    _configure()

    from stapel_tools.codegen import emit_errors, emit_flows, emit_schema

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    paths = emit_schema(out / "schema.json")
    flows = emit_flows(out / "flows.json")
    errors = emit_errors(out / "errors.json")

    print(
        f"stapel-webhooks contract: {paths} paths, {flows} flows, "
        f"{errors} error keys -> {out}/",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
