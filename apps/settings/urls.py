from django.urls import path

from . import views

urlpatterns = [
    path("", views.settings_index, name="settings_index"),
    path("dispatch-alerts/", views.dispatch_alerts, name="dispatch_alerts"),
    path("notifications/", views.notifications, name="notifications"),
    path("vehicle-types/", views.vehicle_type_list, name="vehicle_type_list"),
    path("vehicle-types/new/", views.vehicle_type_create, name="vehicle_type_create"),
    path("vehicle-types/<int:pk>/", views.vehicle_type_edit, name="vehicle_type_edit"),
    path("vehicle-types/<int:pk>/delete/", views.vehicle_type_delete, name="vehicle_type_delete"),
    path("service-types/", views.service_type_list, name="service_type_list"),
    path("service-types/new/", views.service_type_create, name="service_type_create"),
    path("service-types/<int:pk>/", views.service_type_edit, name="service_type_edit"),
    path("service-types/<int:pk>/delete/", views.service_type_delete, name="service_type_delete"),
    path("renewal-types/", views.renewal_type_list, name="renewal_type_list"),
    path("renewal-types/new/", views.renewal_type_create, name="renewal_type_create"),
    path("renewal-types/<int:pk>/", views.renewal_type_edit, name="renewal_type_edit"),
    path("renewal-types/<int:pk>/delete/", views.renewal_type_delete, name="renewal_type_delete"),
]
