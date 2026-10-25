from django.urls import path

from . import views

urlpatterns = [
    # APC-47 — the customer card vault
    path("contacts/<int:contact_pk>/cards/setup/", views.card_setup, name="billing_card_setup"),
    path(
        "contacts/<int:contact_pk>/cards/complete/",
        views.card_complete,
        name="billing_card_complete",
    ),
    path("cards/<int:pk>/default/", views.card_default, name="billing_card_default"),
    path("cards/<int:pk>/remove/", views.card_remove, name="billing_card_remove"),
    path(
        "contacts/<int:contact_pk>/account/",
        views.account_create,
        name="billing_account_create",
    ),
    path("accounts/<int:pk>/", views.account_update, name="billing_account_update"),
    path("accounts/<int:pk>/sync/", views.account_sync, name="billing_account_sync"),
    path("groups/<int:pk>/sync/", views.group_sync, name="billing_group_sync"),
    path("accounts/<int:pk>/groups/", views.group_add, name="billing_group_add"),
    path("groups/<int:pk>/", views.group_update, name="billing_group_update"),
    path("groups/<int:pk>/default/", views.group_default, name="billing_group_default"),
    path("groups/<int:pk>/delete/", views.group_delete, name="billing_group_delete"),
]
