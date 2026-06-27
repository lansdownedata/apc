from django.urls import path

from . import views

urlpatterns = [
    path("", views.lead_list, name="lead_list"),
    path("new/", views.lead_create, name="lead_create"),
    path("<int:pk>/", views.lead_detail, name="lead_detail"),
    path("<int:pk>/update/", views.lead_update, name="lead_update"),
    path("<int:pk>/mark-lost/", views.lead_mark_lost, name="lead_mark_lost"),
    path("<int:pk>/reopen/", views.lead_reopen, name="lead_reopen"),
]
