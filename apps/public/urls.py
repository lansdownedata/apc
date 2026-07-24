from django.urls import path

from . import views

app_name = "public"

urlpatterns = [
    path("", views.home, name="home"),
    path("bookings/", views.bookings, name="bookings"),
    path("bookings/thanks/", views.booking_thanks, name="booking_thanks"),
    path("about-us/", views.about, name="about"),
    path("fleet/", views.fleet, name="fleet"),
    path("contact/", views.contact, name="contact"),
    path("privacy-policy/", views.privacy, name="privacy"),
]
