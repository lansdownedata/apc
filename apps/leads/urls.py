from django.urls import path

from . import views

urlpatterns = [
    path("", views.lead_list, name="lead_list"),
    path("new/", views.lead_create, name="lead_create"),
    path("<int:pk>/", views.lead_detail, name="lead_detail"),
    path("<int:pk>/update/", views.lead_update, name="lead_update"),
    path("<int:pk>/mark-lost/", views.lead_mark_lost, name="lead_mark_lost"),
    path("<int:pk>/reopen/", views.lead_reopen, name="lead_reopen"),
    path("<int:pk>/send-quote/", views.lead_send_quote, name="lead_send_quote"),
    # public (no login) — Stripe redirect targets, keyed by signed token
    path(
        "quote/deposit/success/<str:token>/",
        views.quote_deposit_success,
        name="quote_deposit_success",
    ),
    path(
        "quote/deposit/cancel/<str:token>/",
        views.quote_deposit_cancel,
        name="quote_deposit_cancel",
    ),
]
