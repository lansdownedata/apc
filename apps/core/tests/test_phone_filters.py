import pytest

from apps.core.templatetags.phone_filters import phone_display


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("+16175550207", "(617) 555-0207"),
        ("+442079460958", "+44 20 7946 0958"),
    ],
)
def test_formats_e164_for_humans(raw, expected):
    assert phone_display(raw) == expected


@pytest.mark.parametrize("raw", ["", None, "(734) 069-1777", "not a phone"])
def test_passes_through_what_it_cannot_parse(raw):
    """Legacy and unparseable rows must still render, never blank out."""
    assert phone_display(raw) == ("" if raw is None else raw)


def test_national_format_for_us_numbers_only():
    """A US number renders without the +1; anything else keeps its country code."""
    assert "+1" not in phone_display("+12025550100")
    assert phone_display("+442079460958").startswith("+44")
