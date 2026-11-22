from django.urls import path

from . import views

app_name = "customers"

urlpatterns = [
    path("", views.home, name="home"),
    path("trips/", views.trips, name="trips"),
    path("trips/<int:pk>/", views.trip, name="trip"),
    path("trips/<int:pk>/receipt.pdf", views.receipt, name="receipt"),
    path("trips/<int:pk>/change/", views.ask_change, name="ask_change"),
    path("trips/<int:pk>/cancel/", views.ask_cancel, name="ask_cancel"),
    path("trips/export.csv", views.export, name="export"),
    path("trips/reference/", views.set_reference, name="set_reference"),
    path("request/", views.request_trip, name="request_trip"),
    path("login/", views.login, name="login"),
    path("logout/", views.logout, name="logout"),
    path("signup/", views.signup, name="signup"),
    path("signup/<str:token>/", views.signup_confirm, name="signup_confirm"),
    path("register/<uidb64>/<token>/", views.set_password, name="register"),
    path("password/reset/", views.password_reset, name="password_reset"),
    path("password/<uidb64>/<token>/", views.set_password, name="set_password"),
]
