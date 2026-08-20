from django.urls import path

from . import views

urlpatterns = [
    path("", views.dispatch_board, name="dispatch_board"),
    path("<int:pk>/panel/", views.assign_panel, name="dispatch_assign_panel"),
]
