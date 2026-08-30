from django.urls import path

from . import views

# Namespaced: the vendor roster already owns the bare names driver_create / driver_edit.
app_name = "fleet"

urlpatterns = [
    path("", views.driver_list, name="driver_list"),
    path("vehicles/", views.vehicle_list, name="vehicle_list"),
    path("drivers/new/", views.driver_create, name="driver_create"),
    path("drivers/<int:pk>/", views.driver_detail, name="driver_detail"),
    path("drivers/<int:pk>/edit/", views.driver_edit, name="driver_edit"),
    path(
        "drivers/<int:pk>/address/update/",
        views.driver_address_update,
        name="driver_address_update",
    ),
    path("vehicles/new/", views.vehicle_create, name="vehicle_create"),
    path("vehicles/<int:pk>/", views.vehicle_detail, name="vehicle_detail"),
    path("vehicles/<int:pk>/edit/", views.vehicle_edit, name="vehicle_edit"),
    path(
        "drivers/<int:pk>/renewals/new/",
        views.driver_renewal_create,
        name="driver_renewal_create",
    ),
    path(
        "vehicles/<int:pk>/renewals/new/",
        views.vehicle_renewal_create,
        name="vehicle_renewal_create",
    ),
    path("renewals/<int:pk>/", views.renewal_edit, name="renewal_edit"),
    path("renewals/<int:pk>/renew/", views.renewal_renew, name="renewal_renew"),
    path("renewals/<int:pk>/delete/", views.renewal_delete, name="renewal_delete"),
]
