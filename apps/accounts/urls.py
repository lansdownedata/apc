from django.urls import path

from . import views

urlpatterns = [
    path("", views.user_list, name="user_list"),
    path("invite/", views.user_invite, name="user_invite"),
    path("<int:pk>/", views.user_detail, name="user_detail"),
    path("<int:pk>/resend/", views.user_resend_invite, name="user_resend_invite"),
    path("<int:pk>/revoke/", views.user_revoke_invite, name="user_revoke_invite"),
    path("<int:pk>/active/", views.user_set_active, name="user_set_active"),
]
