from django.urls import path

from . import views

app_name = "audit"

urlpatterns = [
    path("trip/<int:pk>/", views.history, {"kind": "trip"}, name="trip"),
    path("order/<int:pk>/", views.history, {"kind": "order"}, name="order"),
    # contact | company | vendor; any other kind is a 404 in the view.
    path("<str:kind>/<int:pk>/", views.history, name="profile"),
]
