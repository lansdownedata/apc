from django.urls import path

from . import views

urlpatterns = [
    path("", views.orders_list, name="orders_list"),
    path("<int:lead_id>/refund/", views.order_refund, name="order_refund"),
    path("<int:lead_id>/cancel-refund/", views.order_cancel_refund, name="order_cancel_refund"),
    path("<int:lead_id>/retry-balance/", views.order_retry_balance, name="order_retry_balance"),
    path("<int:lead_id>/mark-paid/", views.order_mark_paid, name="order_mark_paid"),
]
