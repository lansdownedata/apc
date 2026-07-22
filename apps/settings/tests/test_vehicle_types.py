"""Vehicle Types CRUD — owner-admin only, guarded delete."""

import re

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.leads.factories import VehicleTypeFactory
from apps.leads.models import VehicleType

pytestmark = pytest.mark.django_db

# A 1x1 transparent GIF — the smallest valid image Pillow will accept.
TINY_GIF = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!"
    b"\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
)


def test_list_requires_owner_admin(client):
    client.force_login(UserFactory(role="agent"))
    assert client.get(reverse("vehicle_type_list")).status_code == 403


def test_owner_admin_sees_the_list(client):
    client.force_login(UserFactory(role="owner_admin"))
    # "Vintage Trolley" is deliberately NOT one of the six names seeded by
    # reservations/0003_seed_vehicles, so this only passes if the created row
    # actually renders (a seeded name would pass even with the factory line deleted).
    VehicleTypeFactory(name="Vintage Trolley")
    response = client.get(reverse("vehicle_type_list"))
    assert response.status_code == 200
    assert b"Vintage Trolley" in response.content


def test_create_with_an_image(client, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    client.force_login(UserFactory(role="owner_admin"))
    response = client.post(
        reverse("vehicle_type_create"),
        {
            "name": "28-Passenger Mini Bus",
            "capacity": 28,
            "description": "Perfect for wedding parties.",
            "sort_order": 3,
            "active": "on",
            "image": SimpleUploadedFile("bus.gif", TINY_GIF, content_type="image/gif"),
        },
    )
    assert response.status_code == 302
    vt = VehicleType.objects.get(name="28-Passenger Mini Bus")
    assert vt.capacity == 28
    assert "vehicle-types/" in vt.image.name


def test_create_without_an_image_is_allowed(client):
    client.force_login(UserFactory(role="owner_admin"))
    client.post(
        reverse("vehicle_type_create"),
        {"name": "Trolley", "capacity": 30, "description": "", "sort_order": 0, "active": "on"},
    )
    assert VehicleType.objects.filter(name="Trolley").exists()


def test_delete_deactivates_when_in_use(client):
    from apps.reservations.factories import ReservationFactory

    client.force_login(UserFactory(role="owner_admin"))
    vt = VehicleTypeFactory(name="In Use SUV")
    ReservationFactory(vehicle=vt)
    client.post(reverse("vehicle_type_delete", args=[vt.pk]))
    vt.refresh_from_db()
    assert vt.active is False, "a referenced type must survive so old quotes still render"


def test_delete_removes_when_unused(client):
    client.force_login(UserFactory(role="owner_admin"))
    vt = VehicleTypeFactory(name="Never Booked")
    client.post(reverse("vehicle_type_delete", args=[vt.pk]))
    assert not VehicleType.objects.filter(pk=vt.pk).exists()


def test_edit_page_delete_form_is_not_nested(client):
    """A <form> start tag encountered while another form is still open is a parse
    error under the HTML5 spec — a spec-compliant (browser) parser drops the token
    and no delete-form element is created in the DOM. That silently kills the Delete
    button (`getElementById('delete-form')` returns null), and a POST-directly test
    against the delete view can't catch it because it never renders the template.

    The page also carries an unrelated, already-closed <form> (base.html's logout
    form in the nav), so we can't just count "<form " tags — instead we walk the
    open/close tags in document order and check how many are still open at the
    point the delete form's own start tag appears.
    """
    client.force_login(UserFactory(role="owner_admin"))
    vt = VehicleTypeFactory(name="Vintage Trolley")
    html = client.get(reverse("vehicle_type_edit", args=[vt.pk])).content.decode()

    tags = re.finditer(r"<form\b[^>]*>|</form>", html)
    depth = 0
    depth_at_delete_form_open = None
    for match in tags:
        tag = match.group()
        if tag.startswith("</form"):
            depth -= 1
        else:
            if 'id="delete-form"' in tag:
                depth_at_delete_form_open = depth
            depth += 1

    assert depth_at_delete_form_open is not None, "delete form was not found in the response"
    assert depth_at_delete_form_open == 0, (
        "the delete form's <form> tag opens while another form is still open — "
        "a real browser drops it, so #delete-form never exists in the DOM"
    )


def test_form_carries_multipart_enctype(client):
    """django.test.Client encodes multipart itself whenever a file is in the data
    dict, so a POST-only test never proves the template emits the attribute. Without
    it a real browser posts urlencoded and the file vanishes with no error."""
    client.force_login(UserFactory(role="owner_admin"))
    response = client.get(reverse("vehicle_type_create"))
    assert b'enctype="multipart/form-data"' in response.content


def test_create_page_uses_the_custom_uploader_not_native_chrome(client):
    """The Photo field should render the styled dropzone (an Alpine imageUpload
    component driving a still-real, still-submittable <input type=file name=image>),
    not the browser's native "Choose File / No file chosen" control."""
    client.force_login(UserFactory(role="owner_admin"))
    html = client.get(reverse("vehicle_type_create")).content.decode()
    assert 'x-data="imageUpload(' in html, "modern uploader component is missing"
    # the real input must survive, or the upload silently stops working
    assert 'type="file"' in html
    assert 'name="image"' in html
    # exactly one file input — a stray one signals a leaked/duplicated widget
    assert html.count('type="file"') == 1


def test_uploader_template_comment_does_not_leak_into_the_page(client):
    """Django's {# #} comments are single-line only (the lexer regex is not DOTALL),
    so a multi-line one renders its body — including any literal <input> markup —
    as real HTML. This exact class of bug shipped once already; guard against it."""
    client.force_login(UserFactory(role="owner_admin"))
    html = client.get(reverse("vehicle_type_create")).content.decode()
    assert "{#" not in html and "#}" not in html, "a template comment leaked into the page"
    assert "still submittable" not in html, "uploader comment body rendered as text"


def test_edit_page_has_no_clearable_fileinput_chrome(client, settings, tmp_path):
    """Switching the widget to FileInput drops Django's ClearableFileInput markup —
    the "Currently: … Clear" checkbox (id image-clear_id) that reads as old-school.
    Its absence on an edit page (which has an existing image) proves the plain widget
    is in use."""
    settings.MEDIA_ROOT = tmp_path
    client.force_login(UserFactory(role="owner_admin"))
    vt = VehicleType.objects.create(
        name="Photographed Coach",
        capacity=40,
        image=SimpleUploadedFile("coach.gif", TINY_GIF, content_type="image/gif"),
    )
    html = client.get(reverse("vehicle_type_edit", args=[vt.pk])).content.decode()
    assert "image-clear" not in html, "ClearableFileInput chrome leaked into the page"


def test_edit_page_seeds_the_uploader_with_the_existing_image(client, settings, tmp_path):
    """Editing a type that already has a photo should show it inside the uploader as
    the initial preview, so the admin sees what the customer will."""
    settings.MEDIA_ROOT = tmp_path
    client.force_login(UserFactory(role="owner_admin"))
    vt = VehicleType.objects.create(
        name="Previewed SUV",
        capacity=6,
        image=SimpleUploadedFile("suv.gif", TINY_GIF, content_type="image/gif"),
    )
    html = client.get(reverse("vehicle_type_edit", args=[vt.pk])).content.decode()
    assert vt.image.url in html, "existing photo not seeded into the uploader preview"


def test_list_thumbnails_show_the_whole_vehicle(client, settings, tmp_path):
    """Vehicle photos are landscape; a square object-cover crop lops off the front and
    rear. The list must contain the full vehicle, so the thumbnail is object-contain."""
    settings.MEDIA_ROOT = tmp_path
    client.force_login(UserFactory(role="owner_admin"))
    VehicleType.objects.create(
        name="Landscape Limo",
        capacity=10,
        image=SimpleUploadedFile("limo.gif", TINY_GIF, content_type="image/gif"),
    )
    html = client.get(reverse("vehicle_type_list")).content.decode()
    assert "object-contain" in html
    assert "object-cover" not in html, "object-cover crops the vehicle out of frame"
