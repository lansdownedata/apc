from django.urls import path

from . import views

app_name = "integrations"

urlpatterns = [
    path("podium/authorize/", views.podium_authorize, name="podium_authorize"),
    path("podium/callback/", views.podium_callback, name="podium_callback"),
]
