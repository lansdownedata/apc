from django.urls import path

from . import views

urlpatterns = [
    path("save/", views.reservation_save, name="reservation_save"),
    path("flights/verify/", views.flight_verify, name="flight_verify"),
    path("<int:pk>/duplicate/", views.reservation_duplicate, name="reservation_duplicate"),
    path("<int:pk>/reverse/", views.reservation_reverse, name="reservation_reverse"),
    path("<int:pk>/return/", views.reservation_return, name="reservation_return"),
    path("copy-dates/", views.reservation_copy_dates, name="reservation_copy_dates"),
    path("<int:pk>/delete/", views.reservation_delete, name="reservation_delete"),
    path(
        "<int:pk>/group/delete/",
        views.reservation_group_delete,
        name="reservation_group_delete",
    ),
]
