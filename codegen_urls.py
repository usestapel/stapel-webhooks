"""Canonical-prefix URLconf for contract emission (contract-pipeline.md §2).

The host mounts webhooks at ``path("webhooks/",
include("stapel_webhooks.urls"))``, and the module's own ``urls.py`` bakes
the ``api/v1/`` segment in (api-versioning.md §2 — the version segment is
part of the contract), so the resulting public prefix is
``/webhooks/api/v1/…``. This URLconf reproduces that mount exactly, so
drf-spectacular emits ``/webhooks/api/v1/…`` paths and
``generate_flow_docs`` resolves flow endpoints to the same.

Declared separately from the test urlconf so the contract-emission mount can
never silently drift from the module's documented public mount recipe.
"""
from django.urls import include, path

urlpatterns = [
    path("webhooks/", include("stapel_webhooks.urls")),
]
