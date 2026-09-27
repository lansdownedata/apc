from django.urls import path

from . import review_views as views

urlpatterns = [
    path("", views.trip_review_list, name="trip_review_list"),
    path("<int:lead_id>/", views.trip_review_order, name="trip_review_order"),
    path("<int:lead_id>/charge/", views.trip_review_charge, name="trip_review_charge"),
    path("<int:lead_id>/send-link/", views.trip_review_send_link, name="trip_review_send_link"),
    path("trip/<int:pk>/", views.trip_review_trip, name="trip_review_trip"),
    path("trip/<int:pk>/save/", views.trip_review_save, name="trip_review_save"),
    path("trip/<int:pk>/complete/", views.trip_review_complete, name="trip_review_complete"),
    path("trip/<int:pk>/issues/add/", views.trip_review_issue_add, name="trip_review_issue_add"),
    path(
        "issues/<int:pk>/resolve/",
        views.trip_review_issue_resolve,
        name="trip_review_issue_resolve",
    ),
]
