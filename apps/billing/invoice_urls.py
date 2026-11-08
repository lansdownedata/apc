from django.urls import path

from . import invoice_views as views

urlpatterns = [
    path("", views.billing_list, name="billing"),
    path("groups/<int:pk>/invoices/", views.invoice_create, name="billing_invoice_create"),
    path("invoices/<int:pk>/void/", views.invoice_void, name="billing_invoice_void"),
    path("invoices/<int:pk>/sync/", views.invoice_sync, name="billing_invoice_sync"),
]
