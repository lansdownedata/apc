from django.urls import path

from . import views

urlpatterns = [
    path("", views.contact_list, name="contact_list"),
    path("<int:pk>/", views.contact_detail, name="contact_detail"),
    path("<int:pk>/update/", views.contact_update, name="contact_update"),
    path("companies/<int:pk>/", views.company_detail, name="company_detail"),
    path("companies/<int:pk>/update/", views.company_update, name="company_update"),
]
