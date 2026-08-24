from django.urls import include, path

urlpatterns = [
    path("webhooks/", include("stapel_webhooks.urls")),
]
