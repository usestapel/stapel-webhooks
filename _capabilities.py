"""stapel-webhooks capabilities.json emitter — thin shim over stapel_tools.capabilities."""
from pathlib import Path

from stapel_tools.capabilities import axis_group_rules, run_capabilities_cli

#: The CTO-facing switches: what the reaction layer listens to, where a
#: delivery runs, and whether the SSRF guard is on. Everything else in DEFAULTS
#: is tuning (ladder, timeouts, retention, page sizes) or an extension seam
#: (DELIVERY_TYPES / TRANSPORT / SIGNER / MATCHER, curated in
#: docs/capabilities.meta.json).
AXES = ("WATCH_CATALOG", "DISPATCH_MODE", "ALLOW_INSECURE_TARGETS")


def axis_config():
    """``(defaults, is_axis, axis_group)`` the emitter and its tests share."""
    from stapel_webhooks.conf import DEFAULTS

    return (
        DEFAULTS,
        lambda key: key in AXES,
        axis_group_rules(
            exact={
                "WATCH_CATALOG": "webhooks.events",
                "DISPATCH_MODE": "webhooks.delivery",
                "ALLOW_INSECURE_TARGETS": "webhooks.security",
            }
        ),
    )


def main(argv=None):
    from stapel_webhooks._codegen import _configure

    _configure()
    from stapel_webhooks.urls_v1 import GATE_REGISTRY

    defaults, is_axis, axis_group = axis_config()
    return run_capabilities_cli(
        argv,
        repo=Path(__file__).resolve().parent,
        canonical_prefix="/webhooks/api/v1",
        defaults=defaults,
        registry=GATE_REGISTRY,
        is_axis=is_axis,
        axis_group=axis_group,
        prog="stapel-webhooks-capabilities",
    )


if __name__ == "__main__":
    raise SystemExit(main())
