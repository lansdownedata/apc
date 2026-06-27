from django.urls import path

from . import views

urlpatterns = [
    path("", views.lead_list, name="lead_list"),
    path("new/", views.lead_create, name="lead_create"),
    path("<int:pk>/", views.lead_detail, name="lead_detail"),
]
