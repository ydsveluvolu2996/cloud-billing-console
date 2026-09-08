from django import template
register = template.Library()

@register.filter
def money(value):
    return '—' if value is None else f'{value:,.2f}'

@register.filter
def percent(value):
    return '—' if value is None else f'{value:+.1f}%'
