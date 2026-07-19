from django.urls import path

from . import views

urlpatterns = [
    path("", views.contact_list, name="contact_list"),
    path("<int:pk>/", views.contact_detail, name="contact_detail"),
    path("<int:pk>/phones/add/", views.contact_phone_add, name="contact_phone_add"),
    path(
        "<int:pk>/phones/<int:phone_pk>/primary/",
        views.contact_phone_primary,
        name="contact_phone_primary",
    ),
    path(
        "<int:pk>/phones/<int:phone_pk>/delete/",
        views.contact_phone_delete,
        name="contact_phone_delete",
    ),
]
