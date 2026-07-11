from django.urls import path

from . import views

urlpatterns = [
    path("", views.inbox, name="inbox"),
    path("<int:pk>/send/", views.inbox_send, name="inbox_send"),
]
