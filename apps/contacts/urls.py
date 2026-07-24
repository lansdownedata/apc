from django.urls import path

from . import views

urlpatterns = [
    path("", views.contact_list, name="contact_list"),
    path("create/", views.contact_create, name="contact_create"),
    path("<int:pk>/", views.contact_detail, name="contact_detail"),
    path("<int:pk>/update/", views.contact_update, name="contact_update"),
    path(
        "<int:pk>/address/<slug:slot>/update/",
        views.contact_address_update,
        name="contact_address_update",
    ),
    path("companies/<int:pk>/", views.company_detail, name="company_detail"),
    path("companies/<int:pk>/update/", views.company_update, name="company_update"),
]
