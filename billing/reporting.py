from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal
from django.db.models import Sum
from django.db.models.functions import TruncMonth
from django.utils import timezone
from .models import Cost, Customer


def report(params):
    today = timezone.now().date()
    try:
        start = date.fromisoformat(params.get('start', today.replace(day=1).isoformat()))
        end = date.fromisoformat(params.get('end', today.isoformat()))
    except (ValueError, TypeError):
        raise ValueError('Choose valid start and end dates.')
    if start > end or (end - start).days > 731 or end > today:
        raise ValueError('Choose a date range of up to two years ending today or earlier.')
    currency = params.get('currency', 'USD')
    metric = params.get('metric', 'unblended')
    if metric not in ('unblended', 'amortized'):
        raise ValueError('Choose a valid cost basis.')
    customers = Customer.objects.all()
    cid = params.get('customer', '')
    if cid:
        try:
            customers = customers.filter(pk=cid)
            list(customers[:1])
        except Exception:
            raise ValueError('Choose a valid customer.')
    base = Cost.objects.filter(customer__in=customers, currency=currency)
    if params.get('account'):
        base = base.filter(account_id=params['account'])
    if params.get('service'):
        base = base.filter(service=params['service'])
    selected = base.filter(day__gte=start, day__lte=end)
    total = selected.aggregate(v=Sum(metric))['v'] or Decimal(0)
    days = (end - start).days + 1
    previous = base.filter(day__gte=start-timedelta(days=days), day__lt=start).aggregate(v=Sum(metric))['v']
    change = ((total - previous) / abs(previous) * 100) if previous else None
    yesterday = base.filter(day=today-timedelta(days=1)).aggregate(v=Sum(metric))['v']
    monthly = params.get('granularity') == 'monthly'
    if monthly:
        grouped = selected.annotate(period=TruncMonth('day')).values('period').annotate(amount=Sum(metric)).order_by('period')
        points = [{'label': x['period'].strftime('%b %Y'), 'amount': float(x['amount'])} for x in grouped]
    else:
        values = {x['day']: x['amount'] for x in selected.values('day').annotate(amount=Sum(metric))}
        points = [{'label': (start+timedelta(days=i)).isoformat(), 'amount': float(values.get(start+timedelta(days=i), 0))} for i in range(days)]
    service_totals = list(selected.values('service').annotate(amount=Sum(metric)).order_by('-amount'))
    for item in service_totals:
        item['share'] = max(0, min(100, float(item['amount'] / total * 100))) if total > 0 else 0
    rows = []
    # Portfolio budgets and forecasts always refer to the current calendar month.
    current = base.filter(day__gte=today.replace(day=1), day__lte=today)
    mtd = {x['customer_id']: x['amount'] for x in current.values('customer_id').annotate(amount=Sum(metric))}
    completed = {x['customer_id']: x['amount'] for x in current.filter(day__lt=today).values('customer_id').annotate(amount=Sum(metric))}
    period = {x['customer_id']: x['amount'] for x in selected.values('customer_id').annotate(amount=Sum(metric))}
    yday = {x['customer_id']: x['amount'] for x in base.filter(day=today-timedelta(days=1)).values('customer_id').annotate(amount=Sum(metric))}
    over = 0
    for customer in customers:
        spend = mtd.get(customer.pk)
        forecast = completed[customer.pk] / (today.day - 1) * monthrange(today.year,today.month)[1] if customer.pk in completed and today.day > 1 else None
        budget = customer.budget if customer.currency == currency else None
        over_budget = budget is not None and spend is not None and spend > budget
        over += int(over_budget)
        rows.append({'customer': customer, 'period': period.get(customer.pk), 'mtd': spend, 'forecast': forecast,
                     'yesterday': yday.get(customer.pk), 'budget': budget, 'over_budget': over_budget,
                     'budget_percent': min(100, max(0, float(spend / budget * 100))) if budget and spend is not None else 0,
                     'accounts': base.filter(customer=customer).values('account_id').distinct().count()})
    return {'start': start, 'end': end, 'today': today, 'currency': currency, 'metric': metric,
            'granularity': 'monthly' if monthly else 'daily', 'total': total, 'previous': previous,
            'change': change, 'yesterday': yesterday, 'points': points, 'services': service_totals[:10],
            'rows': rows, 'over_budget': over, 'customer_count': customers.count(),
            'account_count': base.values('account_id').distinct().count(), 'has_data': selected.exists(),
            'estimated': selected.filter(estimated=True).exists(), 'selected': selected,
            'filter_customer': cid, 'filter_account': params.get('account',''), 'filter_service': params.get('service','')}
