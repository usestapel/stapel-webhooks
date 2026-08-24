"""Print what this deployment can react to.

The same answer the ``webhooks.event_catalog`` comm Function and the
``/event-catalog`` endpoint give — available before either is reachable,
which is when an operator most wants it ("is my module's event actually
subscribable here?").
"""
import json

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "List every subscribable event, from installed modules' schemas/emits."

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", help="Machine-readable output.")

    def handle(self, *args, **options):
        from stapel_webhooks.actions import watched_events
        from stapel_webhooks.catalog import event_catalog

        catalog = event_catalog(refresh=True)
        watched = watched_events()
        rows = [
            {
                "event": name,
                "module": catalog.get(name, {}).get("module", ""),
                "properties": catalog.get(name, {}).get("properties", []),
            }
            for name in sorted(watched)
        ]
        if options["json"]:
            self.stdout.write(json.dumps(rows, indent=2))
            return
        if not rows:
            self.stdout.write("no watched events")
            return
        for row in rows:
            self.stdout.write(f"{row['event']:<48} {row['module']}")
        self.stdout.write(f"\n{len(rows)} subscribable event(s)")
