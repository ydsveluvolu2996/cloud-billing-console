"""Compact, read-only budget presentation; independent limits are never summed."""
from decimal import Decimal
from urllib.parse import urlencode
from django import template
from django.urls import reverse
from django.utils import timezone
from billing.models import BillingSource

register = template.Library()


def usage_item(name, limit, actual, currency, url, stale=False, verified=True, filtered=False):
    percent = actual / limit * 100 if actual is not None and limit is not None and limit > 0 else None
    if limit is None:
        status, tone = 'Not set', 'unknown'
    elif stale:
        status, tone = 'Data stale', 'unknown'
    elif actual is None:
        status, tone = 'Usage unavailable', 'unknown'
    elif not verified:
        status, tone = 'Unverified spend', 'unknown'
    elif actual > limit:
        status, tone = 'Breached', 'breached'
    elif actual == limit:
        status, tone = 'At limit', 'warning'
    elif percent is not None and percent >= 80:
        status, tone = 'Near limit', 'warning'
    else:
        status, tone = 'Within budget', 'healthy'
    fill = min(100, max(0, percent)) if percent is not None else 100 if actual is not None and limit == 0 and actual > 0 else 0
    return {'name': name, 'limit': limit, 'actual': actual, 'currency': currency, 'url': url,
            'percent': percent, 'fill': int(fill), 'status': status, 'tone': tone, 'filtered': filtered,
            'rank': (actual is not None and limit is not None and actual > limit, percent if percent is not None else Decimal('Infinity') if limit == 0 and actual is not None and actual > 0 else Decimal(-1))}


@register.simple_tag
def account_usage(row, currency='USD'):
    items = []
    for b in row.get('aws_budgets', []):
        items.append(usage_item(b.name, b.limit_amount, b.actual_amount if b.actual_unit == b.limit_unit else None,
            b.limit_unit, reverse('imported_budgets') + '?' + urlencode({'q': b.name}),
            stale=getattr(b, 'snapshot_stale', False), filtered=bool(b.filters)))
    for configured in row.get('configured_budgets', []):
        b, evaluation = configured['budget'], configured.get('evaluation')
        # A filtered budget needs its own evaluation; whole-account MTD is not its usage.
        actual = evaluation.actual if evaluation else None if b.filters else row.get('mtd', (row.get('spend') or {}).get('v'))
        stale = bool(evaluation and timezone.now() - evaluation.evaluated_at > BillingSource.STALE_AFTER)
        items.append(usage_item(b.name, configured['amount'], actual, currency, reverse('budget_detail', args=[b.pk]),
            stale=stale, verified=bool(evaluation and evaluation.data_status == 'complete'), filtered=bool(b.filters)))
    items.sort(key=lambda item: item['rank'], reverse=True)
    return {'primary': items[0] if items else None, 'items': items, 'count': len(items)}
