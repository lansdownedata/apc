"""`{% exceptions_summary %}` — the compact exceptions card, droppable on any portal page."""

from django import template

from apps.tasks import exceptions

register = template.Library()


@register.inclusion_tag("components/exceptions_summary.html")
def exceptions_summary() -> dict:
    return {"exceptions": exceptions.dashboard()}
