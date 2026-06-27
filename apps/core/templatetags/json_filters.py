import json

from django import template
from django.utils.safestring import mark_safe

register = template.Library()


@register.filter
def json_string(value):
    return mark_safe(json.dumps("" if value is None else str(value)))
