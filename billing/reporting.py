from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import urlencode
from uuid import UUID
from django.db.models import Count, Max, Min, Sum
from django.db.models.functions import TruncMonth
from django.utils import timezone
from .models import Cost, Customer


def report(params):
    today = timezone.now().date()
    month_start = today.replace(day=1)
    try:
        start = date.fromisoformat(params.get('start', month_start.isoformat()))
        end = date.fromisoformat(params.get('end', today.isoformat()))
    except (ValueError, TypeError):
        raise ValueError('Choose valid start and end dates.')
    if start > end or (end - start).days > 731 or end > today:
        raise ValueError('Choose a date range of up to two years ending today or earlier.')
    currency = params.get('currency', 'USD')
    metric = params.get('metric', 'unblended')
    granularity = params.get('granularity', 'daily')
    if metric not in ('unblended', 'amortized'):
        raise ValueError('Choose a valid cost basis.')
    if granularity not in ('daily', 'monthly'):
        raise ValueError('Choose daily or monthly grouping.')
    if currency not in set(Cost.objects.values_list('currency', flat=True)) | {'USD'}:
        raise ValueError('Choose a currency with imported billing data.')
    customers = Customer.objects.all()
    cid = params.get('customer', '')
    if cid:
        try:
            customers = customers.filter(pk=UUID(str(cid)))
        except (ValueError, TypeError, AttributeError):
            raise ValueError('Choose a valid customer.')
        if not customers.exists():
            raise ValueError('This customer could not be found. Choose another customer.')
    customer_list = list(customers)
    base = Cost.objects.filter(customer__in=customers, currency=currency)
    if params.get('account'):
        base = base.filter(account_id=params['account'])
    if params.get('service'):
        base = base.filter(service=params['service'])
    selected = base.filter(day__gte=start, day__lte=end)
    total = selected.aggregate(v=Sum(metric))['v'] or Decimal(0)
    days = (end - start).days + 1
    previous_start, previous_end = start - timedelta(days=days), start - timedelta(days=1)
    previous = base.filter(day__gte=previous_start, day__lte=previous_end).aggregate(v=Sum(metric))['v']
    change = ((total - previous) / abs(previous) * 100) if previous else None
    yesterday = base.filter(day=today-timedelta(days=1)).aggregate(v=Sum(metric))['v']
    daily = {x['day']: x['amount'] for x in selected.values('day').annotate(amount=Sum(metric))}
    if granularity == 'monthly':
        grouped = selected.annotate(period=TruncMonth('day')).values('period').annotate(amount=Sum(metric))
        values = {x['period']: x['amount'] for x in grouped}
        points, cursor = [], start.replace(day=1)
        while cursor <= end:
            amount = values.get(cursor)
            points.append({'label': cursor.strftime('%b %Y'), 'amount': float(amount) if amount is not None else None})
            cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
    else:
        points = [{'label': (start+timedelta(days=i)).isoformat(),
                   'amount': float(daily[start+timedelta(days=i)]) if start+timedelta(days=i) in daily else None}
                  for i in range(days)]

    filters = {'start': start.isoformat(), 'end': end.isoformat(), 'currency': currency,
               'metric': metric, 'granularity': granularity, 'customer': cid,
               'account': params.get('account', ''), 'service': params.get('service', '')}

    def link(**changes):
        return '/?' + urlencode({k: v for k, v in (filters | changes).items() if v})

    def share(amount):
        return float(amount / total * 100) if total > 0 else None

    service_totals = list(selected.values('service').annotate(amount=Sum(metric), accounts=Count('account_id', distinct=True)).order_by('-amount', 'service'))
    for item in service_totals:
        item.update(share=share(item['amount']), url=link(service=item['service']))
    account_totals = list(selected.values('customer_id', 'customer__name', 'account_id').annotate(amount=Sum(metric), services=Count('service', distinct=True)).order_by('-amount', 'account_id'))
    for item in account_totals:
        item.update(share=share(item['amount']), url=link(customer=str(item['customer_id']), account=item['account_id']))
    # Budgets and projections always use whole-customer costs in the current month.
    current = Cost.objects.filter(customer__in=customers, currency=currency, day__gte=month_start, day__lte=today)
    mtd = {x['customer_id']: x['amount'] for x in current.values('customer_id').annotate(amount=Sum(metric))}
    completed = {x['customer_id']: x['amount'] for x in current.filter(day__lt=today).values('customer_id').annotate(amount=Sum(metric))}
    period = {x['customer_id']: x['amount'] for x in selected.values('customer_id').annotate(amount=Sum(metric))}
    account_counts = {x['customer_id']: x['count'] for x in base.values('customer_id').annotate(count=Count('account_id', distinct=True))}
    rows = []
    for customer in customer_list:
        spend = mtd.get(customer.pk)
        forecast = completed[customer.pk] / (today.day - 1) * monthrange(today.year,today.month)[1] if customer.pk in completed and today.day > 1 else None
        budget = customer.budget if customer.currency == currency else None
        over_budget = budget is not None and spend is not None and spend > budget
        rows.append({'customer': customer, 'period': period.get(customer.pk), 'mtd': spend, 'forecast': forecast,
                     'budget': budget, 'over_budget': over_budget, 'forecast_over': budget is not None and forecast is not None and forecast > budget,
                     'budget_percent': float(spend / budget * 100) if budget and spend is not None else 0,
                     'accounts': account_counts.get(customer.pk, 0), 'url': link(customer=str(customer.pk), account='')})
    rows.sort(key=lambda row: (row['period'] is None, -(row['period'] or 0), row['customer'].name.lower()))
    forecasts = [row['forecast'] for row in rows if row['forecast'] is not None]
    last_month_end = month_start - timedelta(days=1)
    third_month = (last_month_end.replace(day=1) - timedelta(days=1)).replace(day=1)
    presets = [{'label': label, 'url': link(start=a.isoformat(), end=b.isoformat()), 'active': start == a and end == b}
               for label, a, b in [('This month', month_start, today), ('Last month', last_month_end.replace(day=1), last_month_end),
                                   ('Last 3 months', third_month, today)]]
    chips = []
    for key, label in [('customer', customer_list[0].name if cid else ''), ('account', filters['account']), ('service', filters['service'])]:
        if label:
            chips.append({'label': label, 'url': link(**{key: '', **({'account': ''} if key == 'customer' else {})})})
    coverage = base.aggregate(first=Min('day'), last=Max('day'))
    success_times = [c.last_success for c in customer_list if c.last_success]
    now = timezone.now()
    next_sync = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=6 - now.hour % 6)
    return {'start': start, 'end': end, 'today': today, 'currency': currency, 'metric': metric,
            'granularity': granularity, 'total': total, 'previous': previous, 'previous_start': previous_start,
            'previous_end': previous_end, 'change': change, 'yesterday': yesterday, 'points': points,
            'services': [s for s in service_totals if s['amount'] > 0][:5], 'service_rows': service_totals,
            'account_rows': account_totals, 'service_count': len(service_totals), 'rows': rows,
            'over_budget': sum(row['over_budget'] for row in rows), 'forecast_risk': sum(row['forecast_over'] and not row['over_budget'] for row in rows),
            'forecast': sum(forecasts) if forecasts else None, 'forecast_customers': len(forecasts),
            'mtd_total': sum(mtd.values()) if mtd else None, 'customer_count': len(customer_list),
            'connected_count': sum(c.status == 'Connected' for c in customer_list),
            'stale_count': sum(c.status in ('Stale', 'Action required') for c in customer_list),
            'setup_count': sum(not c.role_arn for c in customer_list),
            'queued_count': sum(c.sync_requested for c in customer_list),
            'can_sync': any(c.enabled and c.role_arn for c in customer_list),
            'last_sync': max(success_times) if success_times else None, 'next_sync': next_sync,
            'account_count': len({item['account_id'] for item in account_totals}),
            'has_data': bool(daily), 'estimated': selected.filter(estimated=True).exists(), 'selected': selected,
            'coverage': coverage, 'days': days, 'missing_days': days - len(daily),
            'peak': max(daily.items(), key=lambda x: x[1]) if daily else None,
            'average': total / days if daily else None, 'presets': presets, 'chips': chips,
            'daily_url': link(granularity='daily'), 'monthly_url': link(granularity='monthly'),
            'export_query': urlencode({k: v for k, v in filters.items() if v}),
            'filter_customer': cid, 'filter_account': filters['account'], 'filter_service': filters['service'],
            'selected_customer': customer_list[0] if cid else None,
            'advanced_open': bool(filters['account'] or filters['service'] or metric != 'unblended' or not any(p['active'] for p in presets))}
