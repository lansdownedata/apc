from django.urls import path

from . import views

app_name = "customers"

urlpatterns = [
    path("login/", views.login, name="login"),
    path("register/<uidb64>/<token>/", views.link_page, name="register"),
    path("signup/<str:token>/", views.link_page, name="signup_confirm"),
    path("password/<uidb64>/<token>/", views.link_page, name="set_password"),
]
