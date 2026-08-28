from django.urls import path

from . import views

urlpatterns = [
    path("", views.orders_list, name="orders_list"),
    path("<int:lead_id>/refund/", views.order_refund, name="order_refund"),
    path("<int:lead_id>/cancel-refund/", views.order_cancel_refund, name="order_cancel_refund"),
    path("<int:lead_id>/retry-balance/", views.order_retry_balance, name="order_retry_balance"),
    path("<int:lead_id>/admin-intent/", views.order_admin_intent, name="order_admin_intent"),
    path("<int:lead_id>/admin-complete/", views.order_admin_complete, name="order_admin_complete"),
    path("<int:lead_id>/setup-intent/", views.order_setup_intent, name="order_setup_intent"),
    path("<int:lead_id>/save-card/", views.order_save_card, name="order_save_card"),
    path("<int:lead_id>/charge-saved/", views.order_charge_saved, name="order_charge_saved"),
]
