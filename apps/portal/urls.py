from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("notifications/read-all/", views.notifications_read_all, name="notifications_read_all"),
    path("notifications/<int:pk>/read/", views.notification_read, name="notification_read"),
]
