from django.urls import path
from django.views.generic import RedirectView

from . import views

urlpatterns = [
    path("", views.lead_list, name="lead_list"),
    path("new/", views.lead_create, name="lead_create"),
    path("<int:pk>/", views.lead_detail, name="lead_detail"),
    path("<int:pk>/update/", views.lead_update, name="lead_update"),
    path("<int:pk>/mark-lost/", views.lead_mark_lost, name="lead_mark_lost"),
    path("<int:pk>/mark-booked/", views.lead_mark_booked, name="lead_mark_booked"),
    path("<int:pk>/wedding/", views.lead_wedding_save, name="lead_wedding_save"),
    path("<int:pk>/reopen/", views.lead_reopen, name="lead_reopen"),
    path("<int:pk>/send-quote/", views.lead_send_quote, name="lead_send_quote"),
    path("<int:pk>/reissue-quote/", views.lead_reissue_quote, name="lead_reissue_quote"),
    path("<int:pk>/resend-la/", views.lead_resend_la, name="lead_resend_la"),
    # The 3-D Secure return moved to /quote/<token>/done/ (2026-09-19) — it is a customer
    # URL and belongs with the others, not inside the staff portal. This path is in
    # inboxes already, so it keeps landing somewhere rather than 404ing.
    path(
        "quote/deposit/success/<str:token>/",
        RedirectView.as_view(pattern_name="quote_deposit_success", permanent=True),
        name="quote_deposit_success_legacy",
    ),
]
