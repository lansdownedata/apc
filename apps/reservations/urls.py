from django.urls import path

from . import views

urlpatterns = [
    path("save/", views.reservation_save, name="reservation_save"),
]
