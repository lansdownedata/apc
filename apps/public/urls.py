from django.urls import path

from . import views
from .redirects import perma

app_name = "public"

urlpatterns = [
    path("", views.home, name="home"),
    path("bookings/", views.bookings, name="bookings"),
    path("bookings/thanks/", views.booking_thanks, name="booking_thanks"),
    path("bookings/geocode/", views.geocode, name="geocode"),
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
    path("blogs/", views.blog_index, name="blog_index"),
    # Exact legacy WordPress date-based post URLs kept for SEO link-equity —
    # one explicit route per post, do not collapse into a slug-based catch-all.
    path(
        "2025/03/the-ultimate-guide-to-selecting-the-right-wedding-transportation-for-2025/",
        views._post("wedding-guide-2025.html"),
        name="post_wedding_guide",
    ),
    path(
        "2025/03/all-pro-charter-named-2025-the-knot-best-of-weddings-weddingwire-couples-choice-award-winner/",
        views._post("knot-2025-award.html"),
        name="post_knot_2025",
    ),
    path(
        "2023/11/5-reasons-all-pro-charter-is-your-reliable-transportation-choice/",
        views._post("5-reasons.html"),
        name="post_5_reasons",
    ),
    path(
        "2022/11/5-tips-to-traveling-this-holiday-season/",
        views._post("holiday-tips.html"),
        name="post_holiday_tips",
    ),
    path(
        "2021/01/covid-19-update-all-pro-charter-is-cdc-compliant/",
        views._post("covid-update.html"),
        name="post_covid",
    ),
    path(
        "2021/01/all-pro-charter-named-winner-in-2021-weddingwire-couples-choice-awards/",
        views._post("weddingwire-2021.html"),
        name="post_weddingwire_2021",
    ),
    # Legacy WordPress URLs — 301 to the closest live target to preserve link equity.
    path("trips/", perma("public:services")),
    path("all-pro-charter-ao/", perma("public:home")),
    path("allprocharter-register/", perma("public:bookings")),
    path("category/press-release/", perma("public:blog_index")),
    path("category/uncategorized/", perma("public:blog_index")),
]
