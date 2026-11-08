"""APC-95 — photos on a trip review (spec 2026-10-27 §5): when a trip carries a cleaning-fee
line, the processing pane offers "Add photos" (optional, images only, several at once).
They are kept on the trip review and shown in its history.
"""

from datetime import time, timedelta
from decimal import Decimal
from io import BytesIO

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from apps.accounts.factories import UserFactory
from apps.leads.factories import LeadFactory, VehicleTypeFactory
from apps.leads.models import Lead
from apps.reservations import reviews
from apps.reservations.factories import ReservationFactory, TripExtraFactory
from apps.reservations.models import Reservation, TripReviewPhoto

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    settings.STORAGES = {
        **settings.STORAGES,
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    }


@pytest.fixture
def agent(client):
    user = UserFactory(can_manage_payments=True)
    client.force_login(user)
    return user


def _png(name="seat.png"):
    buf = BytesIO()
    Image.new("RGB", (2, 2), (200, 30, 30)).save(buf, format="PNG")
    return SimpleUploadedFile(name, buf.getvalue(), content_type="image/png")


def _trip(*, cleaning=True):
    lead = LeadFactory(status=Lead.Status.BOOKED)
    trip = ReservationFactory(
        lead=lead,
        vehicle=VehicleTypeFactory(name="Sprinter photos", cleaning_fee=Decimal("250")),
        trip_status=Reservation.TripStatus.DONE,
        pickup_date=timezone.localdate() - timedelta(days=1),
        pickup_time=time(10, 0),
        pickup_timezone="America/New_York",
    )
    if cleaning:
        TripExtraFactory(
            reservation=trip, kind="cleaning", label="Cleaning fee", amount=Decimal("250")
        )
    return trip


def _pane(client, trip) -> str:
    return client.get(reverse("processing"), {"trip": trip.pk}).content.decode()


def test_photos_upload_images_only_and_show_in_history(client, agent):
    trip = _trip()
    url = reverse("processing_photos", args=[trip.pk])
    assert "Add photos" in _pane(client, trip)
    assert url in _pane(client, trip)

    ok = client.post(url, {"photos": [_png("seat.png"), _png("floor.png")]})
    assert ok.status_code == 200 and ok.json()["ok"] is True
    photos = list(reviews.review_for(trip).photos.all())
    assert len(photos) == 2
    assert all(p.uploaded_by == agent for p in photos)
    assert photos[0].image.name.startswith("trip-reviews/")

    text = SimpleUploadedFile("notes.txt", b"not an image", content_type="text/plain")
    refused = client.post(url, {"photos": [_png("ok.png"), text]})
    assert refused.status_code == 400
    assert refused.json()["ok"] is False
    assert "image" in refused.json()["error"]
    # Nothing from a refused batch is kept.
    assert TripReviewPhoto.objects.count() == 2

    body = _pane(client, trip)
    for photo in photos:
        assert photo.image.url in body


def test_a_fake_png_is_refused(client, agent):
    trip = _trip()
    fake = SimpleUploadedFile("evil.png", b"<script>", content_type="image/png")

    resp = client.post(reverse("processing_photos", args=[trip.pk]), {"photos": [fake]})

    assert resp.status_code == 400
    assert not TripReviewPhoto.objects.exists()


def test_no_files_is_refused(client, agent):
    trip = _trip()

    resp = client.post(reverse("processing_photos", args=[trip.pk]), {})

    assert resp.status_code == 400
    assert resp.json()["error"] == "Choose one or more photos."


def test_photos_need_a_cleaning_line(client, agent):
    trip = _trip(cleaning=False)

    assert "Add photos" not in _pane(client, trip)
    resp = client.post(reverse("processing_photos", args=[trip.pk]), {"photos": [_png()]})

    assert resp.status_code == 400
    assert not TripReviewPhoto.objects.exists()


def test_photos_need_payments_access(client):
    client.force_login(UserFactory(can_manage_payments=False))
    trip = _trip()

    resp = client.post(reverse("processing_photos", args=[trip.pk]), {"photos": [_png()]})

    assert resp.status_code == 403
    assert not TripReviewPhoto.objects.exists()
    assert "Add photos" not in _pane(client, trip)
