"""Reviews board: stats aggregation + page rendering."""

from decimal import Decimal

import pytest
from django.urls import reverse

from apps.leads.factories import LeadFactory
from apps.messaging.factories import ReviewFactory
from apps.messaging.models import Review

pytestmark = pytest.mark.django_db


@pytest.fixture
def agent(django_user_model):
    return django_user_model.objects.create_user(username="agent", password="pw")


def test_review_list_requires_login(client):
    resp = client.get(reverse("review_list"))
    assert resp.status_code == 302
    assert "/login" in resp.url


def test_stats_math(client, agent):
    ReviewFactory(
        rating=4,
        delivery_status=Review.DeliveryStatus.DELIVERED,
        body="Great ride!",
        review_site="Google",
    )
    ReviewFactory(
        rating=5,
        delivery_status=Review.DeliveryStatus.DELIVERED,
        body="Excellent chauffeur.",
        review_site="Yelp",
    )
    ReviewFactory(rating=None, delivery_status=Review.DeliveryStatus.SENT)

    client.force_login(agent)
    resp = client.get(reverse("review_list"))

    assert resp.status_code == 200
    stats = resp.context["stats"]
    assert stats["avg"] == Decimal("4.5")
    assert stats["responses"] == 2
    assert stats["pending"] == 1


def test_pending_counts_only_pending_and_sent_unrated(client, agent):
    ReviewFactory(rating=None, delivery_status=Review.DeliveryStatus.PENDING)
    ReviewFactory(rating=None, delivery_status=Review.DeliveryStatus.SENT)
    ReviewFactory(rating=None, delivery_status=Review.DeliveryStatus.FAILED)
    # A delivered-but-unrated review is not "pending" — it's just unanswered.
    ReviewFactory(rating=None, delivery_status=Review.DeliveryStatus.DELIVERED)

    client.force_login(agent)
    resp = client.get(reverse("review_list"))

    assert resp.context["stats"]["pending"] == 2


def test_no_rated_reviews_gives_none_average(client, agent):
    ReviewFactory(rating=None, delivery_status=Review.DeliveryStatus.PENDING)

    client.force_login(agent)
    resp = client.get(reverse("review_list"))

    stats = resp.context["stats"]
    assert stats["avg"] is None
    assert stats["responses"] == 0


def test_page_renders_rated_review_card(client, agent):
    lead = LeadFactory()
    review = ReviewFactory(
        lead=lead,
        contact=lead.contact,
        rating=5,
        delivery_status=Review.DeliveryStatus.DELIVERED,
        body="Fantastic service, on time and professional.",
        review_site="Google",
        link_clicked=True,
    )

    client.force_login(agent)
    resp = client.get(reverse("review_list"))
    html = resp.content.decode()

    assert lead.contact.name in html
    assert lead.quote_no in html
    assert f'href="{reverse("lead_detail", args=[lead.pk])}"' in html
    assert review.get_delivery_status_display() in html
    assert "Fantastic service, on time and professional." in html
    assert "Google" in html
    # 5 filled stars in the review card, plus 5 in the summary avg (avg == 5.0 since
    # this is the only rated review) — 10 total "on" stars rendered.
    assert html.count("ti-star-filled on") == 10


def test_page_renders_pending_invite_card(client, agent):
    lead = LeadFactory()
    review = ReviewFactory(
        lead=lead,
        contact=lead.contact,
        rating=None,
        delivery_status=Review.DeliveryStatus.SENT,
    )

    client.force_login(agent)
    resp = client.get(reverse("review_list"))
    html = resp.content.decode()

    assert lead.contact.name in html
    assert review.get_delivery_status_display() in html


def test_empty_state(client, agent):
    client.force_login(agent)
    resp = client.get(reverse("review_list"))
    html = resp.content.decode()

    assert "Review invites go out automatically when a trip completes." in html
