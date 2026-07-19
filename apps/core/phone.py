"""Phone-number normalization.

Every phone that enters the system — agent-typed, Podium webhook, seed data — is
normalized to E.164 before storage so that dedupe is a single exact-match lookup.
Podium uses E.164 natively (`+15555555555`), which makes it the natural canonical form.
"""

import phonenumbers


def to_e164(raw: str | None, region: str = "US") -> str | None:
    """Normalize a human-entered phone number to E.164.

    Returns None when the input is blank, unparseable, or not a valid number for the
    region — callers decide whether that is fatal. Never raises.
    """
    if not raw or not raw.strip():
        return None
    try:
        parsed = phonenumbers.parse(raw.strip(), region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
