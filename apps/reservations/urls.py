from django.urls import path

from . import views

urlpatterns = [
    path("save/", views.reservation_save, name="reservation_save"),
    path("flights/verify/", views.flight_verify, name="flight_verify"),
    path("<int:pk>/duplicate/", views.reservation_duplicate, name="reservation_duplicate"),
    path("<int:pk>/delete/", views.reservation_delete, name="reservation_delete"),
]
