import string

from apps.messaging.touchpoint_templates import TEMPLATES, THANK_YOU_WEDDING, render


def test_line_with_all_blank_placeholders_dropped():
    assert render("a\n  Vehicle: {v}\nb", {"v": ""}) == "a\nb"


def test_whitespace_only_value_counts_as_blank():
    assert render("a\n{v}\nb", {"v": "  "}) == "a\nb"


def test_line_with_one_filled_placeholder_kept():
    assert render("{a} ({b})", {"a": "x", "b": ""}) == "x ()"


def test_fixed_text_and_blank_lines_kept():
    assert render("Hi\n\nBye", {}) == "Hi\n\nBye"


def test_escaped_braces_are_not_placeholders():
    assert render("{{literal}}\n{v}", {"v": ""}) == "{literal}"


def test_driver_message_without_vehicle():
    ctx = {k: "x" for k in _fields(TEMPLATES["driver_released"])}
    ctx.update(vehicle_description="", vehicle_number="")
    tpl = TEMPLATES["driver_released"]
    for body in (tpl.email_body, tpl.sms_body):
        out = render(body, ctx)
        assert "Vehicle" not in out
        assert "()" not in out
        assert "Driver" in out or "driver" in out


def test_dispatched_sms_without_vehicle():
    ctx = {k: "x" for k in _fields(TEMPLATES["status_dispatched"])}
    ctx.update(vehicle_description="", vehicle_number="")
    out = render(TEMPLATES["status_dispatched"].sms_body, ctx)
    assert "x, ." not in out
    assert "Driver: x." in out


def _bodies():
    for tpl in [*TEMPLATES.values(), THANK_YOU_WEDDING]:
        for body in (tpl.email_body, tpl.sms_body):
            if body:
                yield body


def _fields(tpl):
    names = set()
    for body in (tpl.email_body, tpl.sms_body, tpl.subject):
        for _, name, _, _ in string.Formatter().parse(body or ""):
            if name:
                names.add(name)
    return names


def test_every_template_keeps_fixed_lines():
    for body in _bodies():
        ctx = {name: "" for _, name, _, _ in string.Formatter().parse(body) if name}
        out = render(body, ctx).split("\n")
        fixed = [ln for ln in body.split("\n") if "{" not in ln and "}" not in ln]
        it = iter(out)
        assert all(ln in it for ln in fixed), body
