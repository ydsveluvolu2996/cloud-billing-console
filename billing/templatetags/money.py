from django import template
register = template.Library()

@register.filter
def money(value):
    return '—' if value is None else f'{value:,.2f}'

@register.filter
def percent(value):
    return '—' if value is None else f'{value:+.1f}%'


@register.filter
def precise_money(value):
    if value is not None and 0 < abs(value) < 0.01:
        return '<0.01' if value > 0 else '−<0.01'
    return money(value)
