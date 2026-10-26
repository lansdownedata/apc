from django.urls import path

from . import processing_views as views

urlpatterns = [
    path("", views.processing_list, name="processing"),
    path("trip/<int:pk>/process/", views.processing_process, name="processing_process"),
    path("trip/<int:pk>/unlock/", views.processing_unlock, name="processing_unlock"),
    path("trip/<int:pk>/charge/", views.processing_add_charge, name="processing_add_charge"),
    path("trip/<int:pk>/refund/", views.processing_refund, name="processing_refund"),
    path("trip/<int:pk>/status/", views.processing_set_status, name="processing_set_status"),
]
