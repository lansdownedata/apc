"""CON-14 / LEAD-15: a refused Podium send reached staff as the raw client error —
`400 POST https://api.podium.com/v4/messages → {"code":"bad_request",…}` — in toasts and in
the order's Message history. `describe_error` is the readable line; the raw text stays in
the server log."""

from apps.integrations.podium import describe_error

RAW = (
    '400 POST https://api.podium.com/v4/messages → {"code":"bad_request",'
    '"message":"Contact opted out","moreInfo":"https://docs.podium.com"}'
)


def test_a_refusal_names_podiums_reason():
    assert describe_error(RAW) == "Podium refused the message: Contact opted out"


def test_a_body_with_no_message_falls_back_to_the_status():
    raw = "503 POST https://api.podium.com/v4/messages → <html>Service Unavailable</html>"
    assert describe_error(raw) == "Podium refused the message (503)."


def test_a_truncated_body_still_finds_the_message():
    assert describe_error(RAW[:110]) == "Podium refused the message: Contact opted out"


def test_several_joined_errors_each_read_cleanly():
    raw = f"{RAW}; {RAW.replace('400', '403', 1)}"
    assert describe_error(raw) == (
        "Podium refused the message: Contact opted out; "
        "Podium refused the message: Contact opted out"
    )


def test_text_that_is_not_a_raw_client_error_is_left_alone():
    for text in ("", "No recipient", "403 missing write_messages", "Podium is not connected."):
        assert describe_error(text) == text
