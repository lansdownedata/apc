import json

from django import template
from django.core.serializers.json import DjangoJSONEncoder
from django.utils.html import escape
from django.utils.safestring import mark_safe

register = template.Library()


@register.filter
def json_string(value):
    """Encode a scalar as a JS string literal, HTML-escaped for attribute context.

    `json.dumps` wraps the value in double quotes, which would close a
    double-quoted HTML attribute (e.g. Alpine ``x-data``) and truncate it. We
    entity-escape the result so the browser decodes it back to a valid JS literal
    when it reads the attribute.
    """
    return mark_safe(escape(json.dumps("" if value is None else str(value))))


@register.filter
def json_attr(value):
    """Encode any JSON-serialisable value (dict, list, scalar) for a double-quoted HTML
    attribute — an Alpine `x-data="flightStatus({ payload: {{ p|json_attr }} })"` object.
    Same escaping contract as `json_string`; dates/datetimes go through DjangoJSONEncoder."""
    return mark_safe(escape(json.dumps(value, cls=DjangoJSONEncoder)))
