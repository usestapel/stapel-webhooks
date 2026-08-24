"""Drop delivery rows past their retention horizon.

Succeeded rows are a log (short horizon); dead rows are evidence (long one).
Both horizons are settings, and ``None`` on either keeps that class forever.
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Delete webhook deliveries past their retention horizon."

    def handle(self, *args, **options):
        from stapel_webhooks.services import purge_deliveries

        counts = purge_deliveries()
        self.stdout.write(
            f"purged succeeded={counts['succeeded']} dead={counts['dead']}"
        )
