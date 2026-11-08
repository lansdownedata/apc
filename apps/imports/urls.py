from django.urls import path

from . import views

urlpatterns = [
    path("", views.history, name="import_history"),
    path("<slug:screen>/template.xlsx", views.template, name="import_template"),
    path("<slug:screen>/export.xlsx", views.export, name="import_export"),
    path("<slug:screen>/upload/", views.upload, name="import_upload"),
    path("<int:pk>/", views.run_detail, name="import_run"),
    path("<int:pk>/confirm/", views.confirm, name="import_confirm"),
    path("<int:pk>/errors.xlsx", views.errors, name="import_errors"),
]
