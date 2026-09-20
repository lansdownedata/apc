from django.urls import path

from . import views

urlpatterns = [
    path(
        "contacts/<int:contact_pk>/account/",
        views.account_create,
        name="billing_account_create",
    ),
    path("accounts/<int:pk>/", views.account_update, name="billing_account_update"),
    path("accounts/<int:pk>/groups/", views.group_add, name="billing_group_add"),
    path("groups/<int:pk>/", views.group_update, name="billing_group_update"),
    path("groups/<int:pk>/default/", views.group_default, name="billing_group_default"),
    path("groups/<int:pk>/delete/", views.group_delete, name="billing_group_delete"),
]
