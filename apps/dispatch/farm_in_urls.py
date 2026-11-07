from django.urls import path

from . import farm_in_views as views

urlpatterns = [
    path("<int:pk>/request-coverage/", views.request_coverage, name="farm_in_request_coverage"),
    path("<int:pk>/send-back/", views.send_back, name="farm_in_send_back"),
    path("<int:pk>/accept/", views.accept, name="farm_in_accept"),
    path("<int:pk>/decline/", views.decline, name="farm_in_decline"),
    path("push/<int:pk>/retry/", views.retry_push, name="farm_in_retry_push"),
]
