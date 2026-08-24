"""Drain the delivery queue once, or in a loop.

The schedulable form lives in ``tasks.drain_deliveries``; this is the same
call for a host on cron, a systemd timer, or an operator watching an
incident recover.
"""
import time

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Attempt every due webhook delivery (one pass, or --loop)."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=None, help="Rows per pass.")
        parser.add_argument(
            "--loop", action="store_true", help="Keep draining until interrupted."
        )
        parser.add_argument(
            "--interval", type=float, default=5.0, help="Seconds between passes with --loop."
        )

    def handle(self, *args, **options):
        from stapel_webhooks.services import drain

        while True:
            counts = drain(limit=options["limit"])
            self.stdout.write(
                "attempted={attempted} succeeded={succeeded} retrying={retrying} "
                "dead={dead} skipped={skipped}".format(**counts)
            )
            if not options["loop"]:
                return
            time.sleep(max(0.0, float(options["interval"])))
