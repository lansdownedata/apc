"""Phone-number normalization.

Every phone that enters the system — agent-typed, Podium webhook, seed data — is
normalized to E.164 before storage so that dedupe is a single exact-match lookup.
Podium uses E.164 natively (`+15555555555`), which makes it the natural canonical form.
"""

import re

import phonenumbers

# Characters allowed in a search term for it to still plausibly be a phone number:
# digits, whitespace, parens, dash, dot, plus. Anything else (letters, "@", ...) means
# the term is free text, not a phone number, however many digits it happens to contain.
_PHONE_LIKE = re.compile(r"^[\d\s()+\-.]+$")


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


def is_phone_like(term: str) -> bool:
    """True when a search `term` plausibly represents a phone number, not free text.

    Rule: after stripping the term of nothing (it must consist ONLY of digits, plus
    common formatting characters — space, `()`, `-`, `.`, `+`), it must contain at
    least 3 digits. This keeps a query like "Suite 5" or "3rd Ave Limo" — which has
    letters in it — from ever being treated as a digit search (which would otherwise
    collapse to "5" / "3" and match nearly every phone number via `icontains`), while
    still accepting realistic phone input such as "(202) 555-0100" or "5550100".
    """
    term = (term or "").strip()
    if not term or not _PHONE_LIKE.match(term):
        return False
    digits = re.sub(r"\D", "", term)
    return len(digits) >= 3
