"""XML sitemap for the public marketing site — lists every crawlable public URL."""

from django.contrib.sitemaps import Sitemap
from django.urls import reverse


class StaticViewSitemap(Sitemap):
    priority = 0.7
    changefreq = "monthly"

    def items(self) -> list[str]:
        return [
            "public:home",
            "public:about",
            "public:fleet",
            "public:contact",
            "public:privacy",
            "public:services",
            "public:service_airport",
            "public:service_corporate",
            "public:service_weddings",
            "public:service_personal",
            "public:reviews",
            "public:rates",
            "public:blog_index",
            "public:post_wedding_guide",
            "public:post_knot_2025",
            "public:post_5_reasons",
            "public:post_holiday_tips",
            "public:post_covid",
            "public:post_weddingwire_2021",
            "public:bookings",
        ]

    def location(self, item: str) -> str:
        return reverse(item)
