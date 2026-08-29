"""Regression guards for vehicle photos on the public marketing pages.

The fleet/rates/home cards frame each vehicle photo in a fixed-height box. Sizing
the `<img>` with `h-full w-full` inside that box makes the browser ignore the
percentage height and lay the image out from its intrinsic aspect ratio at the
full content width — so the image box ends up TALLER than its `h-44`/`h-28`
parent and `overflow-hidden` slices the vehicle's roof and wheels off. The
squarer the source photo, the worse the crop (a 1.5:1 photo lost 52px of 228).
`max-h-full max-w-full` on the image with the padding on the parent contains it
correctly instead.
"""

import re

import pytest

PAGES = ["/fleet/", "/all-pro-charter-rates/", "/"]

# Contained (letterboxed) photos stretched to 100% in both axes. `object-cover`
# heroes legitimately use `h-full w-full` — they are meant to fill and crop.
STRETCHED_IMG = re.compile(rb"<img[^>]*\bh-full w-full object-contain\b[^>]*>")


@pytest.mark.parametrize("url", PAGES)
def test_vehicle_photos_are_not_stretched_to_full_height(client, db, url):
    """No vehicle photo may use `h-full w-full`, which overflows its framed box."""
    resp = client.get(url)
    assert resp.status_code == 200
    offenders = STRETCHED_IMG.findall(resp.content)
    assert not offenders, (
        f"{len(offenders)} image(s) on {url} use `h-full w-full`, which breaks out of "
        f"the fixed-height card box and crops the vehicle. Use "
        f"`max-h-full max-w-full` and put the padding on the parent. "
        f"First offender: {offenders[0][:160].decode(errors='replace')}"
    )


@pytest.mark.parametrize("url", PAGES)
def test_vehicle_photo_frames_center_and_clip_consistently(client, db, url):
    """Every fixed-height photo frame centers its image and clips nothing outside."""
    resp = client.get(url)
    frames = re.findall(rb'<div class="([^"]*\bh-(?:28|32|44)\b[^"]*bg-white[^"]*)"', resp.content)
    assert frames, f"expected at least one vehicle photo frame on {url}"
    for cls in frames:
        decoded = cls.decode()
        assert "overflow-hidden" in decoded, (
            f"photo frame on {url} lacks overflow-hidden: {decoded}"
        )
        assert "p-" in decoded, f"photo frame on {url} carries no padding: {decoded}"
