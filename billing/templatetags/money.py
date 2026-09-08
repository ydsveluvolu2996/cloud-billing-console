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


@register.filter
def price(value, currency='USD'):
    if value is None:
        return '—'
    prefix = {'USD': '$', 'INR': '₹', 'EUR': '€', 'GBP': '£'}.get(currency, currency + ' ')
    sign = '-' if value < 0 else ''
    if 0 < abs(value) < 0.01:
        return sign + '<' + prefix + '0.01'
    return sign + prefix + money(abs(value))


@register.filter
def report_amount(value, unit='USD'):
    if unit in ('USD','INR','EUR','GBP'):
        return price(value,unit)
    return precise_money(value)
