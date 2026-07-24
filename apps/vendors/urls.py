from django.urls import path

from . import views

urlpatterns = [
    path("", views.vendor_list, name="vendor_list"),
    path("new/", views.vendor_create, name="vendor_create"),
    path("<int:pk>/edit/", views.vendor_edit, name="vendor_edit"),
    path("<int:pk>/drivers/new/", views.vendor_detail, name="driver_create"),
    path("drivers/<int:pk>/", views.vendor_detail, name="driver_edit"),
    path("<int:pk>/insurance/new/", views.vendor_detail, name="insurance_create"),
    path("insurance/<int:pk>/", views.vendor_detail, name="insurance_edit"),
    path("<int:pk>/documents/new/", views.vendor_detail, name="document_create"),
    path("<int:pk>/", views.vendor_detail, name="vendor_detail"),
]
