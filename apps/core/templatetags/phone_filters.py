"""Display formatting for stored phone numbers.

Phones are stored canonically as E.164 (see `apps.core.phone.to_e164`), which is
correct for matching and unreadable for humans. This filter is the inverse: it
renders the stored value the way an agent expects to see it, and passes anything
it cannot parse through untouched so legacy rows still display.
"""

import phonenumbers
from django import template

register = template.Library()


@register.filter
def phone_display(value: str | None) -> str:
    """Format an E.164 number for display. Returns the input unchanged if unparseable."""
    if not value:
        return ""
    try:
        parsed = phonenumbers.parse(value, "US")
    except phonenumbers.NumberParseException:
        return value
    if not phonenumbers.is_valid_number(parsed):
        return value
    fmt = (
        phonenumbers.PhoneNumberFormat.NATIONAL
        if parsed.country_code == 1
        else phonenumbers.PhoneNumberFormat.INTERNATIONAL
    )
    return phonenumbers.format_number(parsed, fmt)
