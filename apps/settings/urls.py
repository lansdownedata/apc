from django.urls import path

from . import views

urlpatterns = [
    path("", views.settings_index, name="settings_index"),
    path("vehicle-types/", views.vehicle_type_list, name="vehicle_type_list"),
    path("vehicle-types/new/", views.vehicle_type_create, name="vehicle_type_create"),
    path("vehicle-types/<int:pk>/", views.vehicle_type_edit, name="vehicle_type_edit"),
    path("vehicle-types/<int:pk>/delete/", views.vehicle_type_delete, name="vehicle_type_delete"),
]
