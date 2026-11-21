from django.urls import path

from . import views

app_name = "customers"

urlpatterns = [
    path("", views.home, name="home"),
    path("login/", views.login, name="login"),
    path("logout/", views.logout, name="logout"),
    path("signup/", views.signup, name="signup"),
    path("signup/<str:token>/", views.signup_confirm, name="signup_confirm"),
    path("register/<uidb64>/<token>/", views.set_password, name="register"),
    path("password/reset/", views.password_reset, name="password_reset"),
    path("password/<uidb64>/<token>/", views.set_password, name="set_password"),
]
