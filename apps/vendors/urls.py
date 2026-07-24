from django.urls import path

from . import views

urlpatterns = [
    path("", views.vendor_list, name="vendor_list"),
    path("<int:pk>/", views.vendor_list, name="vendor_detail"),
    path("new/", views.vendor_list, name="vendor_create"),
]
