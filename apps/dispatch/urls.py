from django.urls import path

from . import views

urlpatterns = [
    path("", views.dispatch_board, name="dispatch_board"),
    path("<int:pk>/panel/", views.assign_panel, name="dispatch_assign_panel"),
    path("<int:pk>/offer/", views.offer, name="dispatch_offer"),
    path("<int:pk>/assign/", views.assign, name="dispatch_assign"),
    path("assignment/<int:pk>/resolve/", views.resolve, name="dispatch_resolve"),
]
