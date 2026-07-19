import pytest

from apps.core.phone import to_e164


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("(202) 555-0100", "+12025550100"),
        ("202-555-0100", "+12025550100"),
        ("202.555.0100", "+12025550100"),
        ("2025550100", "+12025550100"),
        ("12025550100", "+12025550100"),
        ("+1 202 555 0100", "+12025550100"),
        ("+12025550100", "+12025550100"),
        ("  (202) 555-0100  ", "+12025550100"),
        ("+44 20 7946 0958", "+442079460958"),
    ],
)
def test_normalizes_to_e164(raw, expected):
    assert to_e164(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", None, "not a phone", "12345", "+1 000 000 0000"])
def test_returns_none_for_unusable_input(raw):
    assert to_e164(raw) is None


def test_respects_region_for_national_format():
    assert to_e164("020 7946 0958", region="GB") == "+442079460958"


def test_already_e164_ignores_region():
    assert to_e164("+12025550100", region="GB") == "+12025550100"
