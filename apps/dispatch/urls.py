from django.urls import path

from . import views

urlpatterns = [
    path("", views.dispatch_board, name="dispatch_board"),
    path("<int:pk>/panel/", views.assign_panel, name="dispatch_assign_panel"),
    path("<int:pk>/offer/", views.offer, name="dispatch_offer"),
    path("<int:pk>/assign/", views.assign, name="dispatch_assign"),
    path("<int:pk>/assign-driver/", views.assign_driver, name="dispatch_assign_driver"),
    path("<int:pk>/status/", views.set_status, name="dispatch_set_status"),
    path(
        "<int:pk>/confirm-customer/",
        views.confirm_customer,
        name="dispatch_confirm_customer",
    ),
    path("assignment/<int:pk>/resolve/", views.resolve, name="dispatch_resolve"),
    path("assignment/<int:pk>/driver-info/", views.driver_info, name="dispatch_driver_info"),
]
