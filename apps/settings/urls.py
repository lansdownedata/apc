from django.urls import path

from . import views

urlpatterns = [
    path("", views.settings_index, name="settings_index"),
    path("dispatch-alerts/", views.dispatch_alerts, name="dispatch_alerts"),
    path("tasks/", views.task_settings, name="task_settings"),
    path("checklist-schedule/", views.checklist_schedule, name="checklist_schedule"),
    path(
        "checklist-schedule/preview/",
        views.checklist_schedule_preview,
        name="checklist_schedule_preview",
    ),
    path("quickbooks/", views.quickbooks_settings, name="quickbooks_settings"),
    path("quickbooks/connect/", views.quickbooks_connect, name="quickbooks_connect"),
    path("quickbooks/disconnect/", views.quickbooks_disconnect, name="quickbooks_disconnect"),
    path("quickbooks/accounts/", views.quickbooks_accounts, name="quickbooks_accounts"),
    path(
        "quickbooks/refresh-accounts/",
        views.quickbooks_refresh_accounts,
        name="quickbooks_refresh_accounts",
    ),
    path(
        "quickbooks/customer-documents/",
        views.quickbooks_customer_docs,
        name="quickbooks_customer_docs",
    ),
    path(
        "quickbooks/refresh-items/", views.quickbooks_refresh_items, name="quickbooks_refresh_items"
    ),
    path(
        "quickbooks/refresh-preferences/",
        views.quickbooks_refresh_preferences,
        name="quickbooks_refresh_preferences",
    ),
    path(
        "quickbooks/refresh-terms/", views.quickbooks_refresh_terms, name="quickbooks_refresh_terms"
    ),
    path("podium/", views.podium_settings, name="podium_settings"),
    path("podium/check-webhook/", views.podium_check_webhook, name="podium_check_webhook"),
    path("messages/", views.messages_list, name="messages"),
    path("messages/master/", views.messages_master, name="messages_master"),
    path("messages/new/", views.messages_new, name="messages_new"),
    path("messages/new/preview/", views.message_preview, name="messages_new_preview"),
    path("messages/new/timing/", views.message_timing_preview, name="messages_new_timing"),
    path("messages/quiet-hours/", views.messages_quiet_hours, name="messages_quiet_hours"),
    path("messages/<str:key>/archive/", views.message_archive, name="message_archive"),
    path("messages/<str:key>/", views.message_edit, name="message_edit"),
    path("messages/<str:key>/toggle/", views.message_toggle, name="message_toggle"),
    path("messages/<str:key>/reset/", views.message_reset, name="message_reset"),
    path("messages/<str:key>/preview/", views.message_preview, name="message_preview"),
    path(
        "messages/<str:key>/timing/",
        views.message_timing_preview,
        name="message_timing_preview",
    ),
    path("pricing/", views.pricing, name="pricing"),
    path("affiliate-scoring/", views.affiliate_scoring, name="affiliate_scoring"),
    path("vehicle-types/", views.vehicle_type_list, name="vehicle_type_list"),
    path("vehicle-types/new/", views.vehicle_type_create, name="vehicle_type_create"),
    path("vehicle-types/<int:pk>/", views.vehicle_type_edit, name="vehicle_type_edit"),
    path("vehicle-types/<int:pk>/delete/", views.vehicle_type_delete, name="vehicle_type_delete"),
    path("pois/", views.poi_list, name="poi_list"),
    path("pois/new/", views.poi_create, name="poi_create"),
    path("pois/<int:pk>/", views.poi_edit, name="poi_edit"),
    path("pois/<int:pk>/delete/", views.poi_delete, name="poi_delete"),
    path("service-types/", views.service_type_list, name="service_type_list"),
    path("service-types/new/", views.service_type_create, name="service_type_create"),
    path("service-types/<int:pk>/", views.service_type_edit, name="service_type_edit"),
    path("service-types/<int:pk>/delete/", views.service_type_delete, name="service_type_delete"),
    path("renewal-types/", views.renewal_type_list, name="renewal_type_list"),
    path("renewal-types/new/", views.renewal_type_create, name="renewal_type_create"),
    path("renewal-types/<int:pk>/", views.renewal_type_edit, name="renewal_type_edit"),
    path("renewal-types/<int:pk>/delete/", views.renewal_type_delete, name="renewal_type_delete"),
    path("affiliate-documents/", views.document_type_list, name="document_type_list"),
    path("affiliate-documents/new/", views.document_type_create, name="document_type_create"),
    path("affiliate-documents/<int:pk>/", views.document_type_edit, name="document_type_edit"),
    path(
        "affiliate-documents/<int:pk>/delete/",
        views.document_type_delete,
        name="document_type_delete",
    ),
]
