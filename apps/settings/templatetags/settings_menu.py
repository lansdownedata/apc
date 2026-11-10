"""`{% settings_menu %}` — the grouped, searchable menu that replaces the sidebar in Settings."""

from django import template

from apps.settings.attention import settings_attention
from apps.settings.menu import current_item, sections

register = template.Library()


@register.inclusion_tag("settings/_menu.html", takes_context=True)
def settings_menu(context: template.Context) -> dict:
    request = context.get("request")
    match = getattr(request, "resolver_match", None)
    # The Overview already built the list; reuse it rather than read the fleet twice.
    attention = context.get("settings_attention")
    if attention is None:
        attention = settings_attention()
    return {
        "sections": sections(),
        "current": current_item(match.url_name if match else None),
        # "Not live" has no page, so it never puts a dot on an item.
        "flagged": {a.url_name: a.title for a in attention if a.url_name},
    }
