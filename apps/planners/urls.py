from django.urls import path

from . import views

app_name = "planners"

urlpatterns = [
    path("", views.weddings, name="weddings"),
    path("login/", views.login, name="login"),
    path("login/code/", views.login_code, name="login_code"),
    path("login/code/resend/", views.login_code_resend, name="login_code_resend"),
    path("logout/", views.logout, name="logout"),
    path("register/<str:uidb64>/<str:token>/", views.set_password, name="register"),
    path("password/reset/", views.password_reset, name="password_reset"),
    path("password/<str:uidb64>/<str:token>/", views.set_password, name="set_password"),
    path("weddings/<int:pk>/", views.wedding, name="wedding"),
    path("weddings/<int:pk>/request/", views.request_change, name="request_change"),
]
