from django.urls import path

from . import views

urlpatterns = [
    path("", views.dispatch_board, name="dispatch_board"),
    path("<int:pk>/panel/", views.assign_panel, name="dispatch_assign_panel"),
    path("<int:pk>/coverage/", views.coverage_controls, name="dispatch_coverage_controls"),
    path("<int:pk>/offer/", views.offer, name="dispatch_offer"),
    path("<int:pk>/assign/", views.assign, name="dispatch_assign"),
    path("<int:pk>/assign-driver/", views.assign_driver, name="dispatch_assign_driver"),
    path("<int:pk>/status/", views.set_status, name="dispatch_set_status"),
    path("<int:pk>/cancel-status/", views.cancel_status, name="dispatch_cancel_status"),
    path("<int:pk>/circling/", views.circling, name="dispatch_circling"),
    path("<int:pk>/needs-farm-out/", views.needs_farm_out, name="dispatch_needs_farm_out"),
    path(
        "<int:pk>/confirm-customer/",
        views.confirm_customer,
        name="dispatch_confirm_customer",
    ),
    path(
        "vendor/<int:pk>/drivers/",
        views.vendor_driver_create,
        name="dispatch_vendor_driver_create",
    ),
    path("assignment/<int:pk>/resolve/", views.resolve, name="dispatch_resolve"),
    path("assignment/<int:pk>/send-back/", views.send_back, name="dispatch_send_back"),
    path(
        "assignment/<int:pk>/resend-link/",
        views.resend_driver_link,
        name="dispatch_resend_driver_link",
    ),
    path(
        "assignment/<int:pk>/cancel-notice/",
        views.cancel_notice,
        name="dispatch_cancel_notice",
    ),
    path("assignment/<int:pk>/driver-info/", views.driver_info, name="dispatch_driver_info"),
]
