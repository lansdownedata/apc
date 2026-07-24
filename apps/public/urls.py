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
    path("services/", views.services, name="services"),
    path("services/airport/", views.service_airport, name="service_airport"),
    path("services/corporate/", views.service_corporate, name="service_corporate"),
    path("services/weddings/", views.service_weddings, name="service_weddings"),
    path("services/personal/", views.service_personal, name="service_personal"),
    path("reviews/", views.reviews, name="reviews"),
    # Exact legacy WordPress slug kept for SEO link-equity — do not shorten to /rates/.
    path("all-pro-charter-rates/", views.rates, name="rates"),
]
