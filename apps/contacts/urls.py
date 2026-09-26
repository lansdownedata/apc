from django.urls import path

from . import views

urlpatterns = [
    path("", views.contact_list, name="contact_list"),
    path("create/", views.contact_create, name="contact_create"),
    path("search/", views.contact_search, name="contact_search"),
    path("<int:pk>/", views.contact_detail, name="contact_detail"),
    path("<int:pk>/update/", views.contact_update, name="contact_update"),
    path("<int:pk>/phones/add/", views.contact_phone_add, name="contact_phone_add"),
    path(
        "<int:pk>/phones/<int:phone_pk>/update/",
        views.contact_phone_update,
        name="contact_phone_update",
    ),
    path(
        "<int:pk>/phones/<int:phone_pk>/texting/",
        views.contact_phone_texting,
        name="contact_phone_texting",
    ),
    path(
        "<int:pk>/phones/<int:phone_pk>/delete/",
        views.contact_phone_delete,
        name="contact_phone_delete",
    ),
    path(
        "<int:pk>/address/<slug:slot>/update/",
        views.contact_address_update,
        name="contact_address_update",
    ),
    path("companies/<int:pk>/", views.company_detail, name="company_detail"),
    path("companies/<int:pk>/update/", views.company_update, name="company_update"),
]
