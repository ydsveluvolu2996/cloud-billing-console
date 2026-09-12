"""Executive spend explanations from authorized additive daily cost facts."""
from calendar import monthrange
from datetime import timedelta
from decimal import Decimal
from urllib.parse import urlencode
from django.db.models import Q, Sum
from django.utils import timezone
from .access import current_access
from .models import AccountAssignment, BillingSource, CollectionPeriod, Cost, Customer
from .scope import resolve


def executive_insights(params, today=None):
    today = today or timezone.now().date()
    start = today.replace(day=1)
    prior_start = (start - timedelta(days=1)).replace(day=1)
    # Compare equal completed-day windows. On the first day there is no basis.
    elapsed = min(today.day - 1, monthrange(prior_start.year, prior_start.month)[1])
    end = start + timedelta(days=elapsed - 1)
    prior_end = prior_start + timedelta(days=elapsed - 1)
    metric, currency = params.get('metric', 'unblended'), params.get('currency', 'USD')
    if metric not in ('unblended', 'amortized'):
        raise ValueError('Choose a valid cost basis.')
    if currency not in set(Cost.objects.values_list('currency', flat=True).distinct()) | {'USD'}:
        raise ValueError('Choose a currency with imported billing data.')
    scope = resolve(params)
    customers = scope.customers.filter(active=True)
    base = scope.costs(Cost.objects.filter(customer__in=customers, currency=currency))
    if params.get('service'):
        base = base.filter(service=params['service'])
    current = base.filter(day__gte=start, day__lte=end)
    previous = base.filter(day__gte=prior_start, day__lte=prior_end)
    assignments = AccountAssignment.objects.filter(customer__in=customers, start__lte=today).filter(Q(end__isnull=True) | Q(end__gt=prior_start))
    if scope.account_id:
        assignments = assignments.filter(account__account_id=scope.account_id)
    source_ids = set(base.filter(day__gte=prior_start, day__lte=today).values_list('source_id', flat=True)) | set(assignments.values_list('account__source_id', flat=True))
    if not scope.account_id:
        source_ids |= set(BillingSource.objects.filter(customer__in=customers, kind__in=['payer', 'standalone']).values_list('pk', flat=True))
    if scope.source:
        source_ids = {scope.source.pk}
    missing_source = None in source_ids
    source_ids.discard(None)
    sources = list(BillingSource.objects.filter(pk__in=source_ids).select_related('customer'))
    periods = {(p.source_id, p.month): p for p in CollectionPeriod.objects.filter(source_id__in=source_ids, month__in=[start, prior_start])}
    # Absence in a currency is not proof of zero spend.
    currency_months = {(sid, day.replace(day=1)) for sid, day in base.filter(day__gte=prior_start, day__lte=today).values_list('source_id', 'day').distinct()}
    access = current_access.get()
    restricted = bool(access and not access.portfolio and any(access.accounts.values()))
    freshness = []
    for source in sources:
        cp, pp = periods.get((source.pk, start)), periods.get((source.pk, prior_start))
        def covered(period, left, right):
            return bool(elapsed and period and period.status == 'complete' and period.first_day and period.first_day <= left and period.last_day and period.last_day >= right and (source.pk, left) in currency_months)
        fresh = bool(source.last_success and timezone.now() - source.last_success <= BillingSource.STALE_AFTER)
        freshness.append({'source_label': 'Authorized account billing feed' if restricted else source.account_id, 'last_import': source.last_success, 'last_day': cp.last_day if cp else None,
                          'current_complete': covered(cp, start, end), 'mtd_complete': covered(cp, start, today - timedelta(days=1)), 'previous_complete': covered(pp, prior_start, prior_end),
                          'state': 'Paused' if not source.enabled else 'Import failed' if source.last_error else 'Fresh' if fresh else 'Stale' if source.last_success else 'Awaiting import'})
    current_complete = bool(freshness) and not missing_source and all(x['current_complete'] for x in freshness)
    comparable = current_complete and all(x['previous_complete'] for x in freshness)
    total = current.aggregate(v=Sum(metric))['v']
    previous_total = previous.aggregate(v=Sum(metric))['v']
    delta = (total or Decimal(0)) - (previous_total or Decimal(0)) if comparable else None
    mtd = base.filter(day__gte=start, day__lt=today).aggregate(v=Sum(metric))['v']
    mtd_complete = bool(freshness) and not missing_source and all(x['mtd_complete'] and x['state'] == 'Fresh' for x in freshness)
    forecast = mtd / (today.day - 1) * monthrange(today.year, today.month)[1] if mtd_complete and mtd is not None and mtd >= 0 and today.day > 1 else None
    dimensions = []
    for title, fields in [('Customer', ('customer_id', 'customer__name')), ('AWS account', ('customer_id', 'customer__name', 'account_id')), ('Service', ('service',))]:
        def groups(qs):
            return {tuple(row[f] for f in fields): row['amount'] for row in qs.values(*fields).annotate(amount=Sum(metric))}
        now_values, old_values = groups(current), groups(previous)
        rows = []
        for key in now_values.keys() | old_values.keys():
            now, old = now_values.get(key), old_values.get(key)
            change = (now or Decimal(0)) - (old or Decimal(0)) if comparable else None
            filters = {'start': str(start), 'end': str(max(start, end)), 'currency': currency, 'metric': metric}
            filters.update({k: params[k] for k in ('customer', 'source', 'account', 'service') if params.get(k)})
            if title in ('Customer', 'AWS account'):
                filters['customer'] = str(key[0])
            if title == 'AWS account':
                filters['account'] = key[2]
            if title == 'Service':
                filters['service'] = key[0]
            rows.append({'name': key[-1], 'customer': key[1] if title == 'AWS account' else '', 'current': now if now is not None else Decimal(0) if comparable else None,
                         'previous': old if old is not None else Decimal(0) if comparable else None,
                         'delta': change, 'percent': change / abs(old) * 100 if change is not None and old else None,
                         'direction': 'Increase' if change is not None and change > 0 else 'Decrease' if change is not None and change < 0 else 'Unchanged' if change == 0 else 'Incomplete',
                         'url': '/portfolio/?' + urlencode(filters)})
        rows.sort(key=lambda row: abs(row['delta']) if comparable else abs(row['current'] or 0), reverse=True)
        dimensions.append({'name': title, 'rows': rows[:10], 'count': len(rows)})
    return {'active_page': 'insights', 'today': today, 'start': start, 'end': end, 'prior_start': prior_start, 'prior_end': prior_end,
            'elapsed': elapsed, 'currency': currency, 'metric': metric, 'scope': scope,
            'customers': Customer.objects.filter(active=True), 'filter_customer': str(scope.customer.pk) if scope.customer else '',
            'filter_source': str(scope.source.pk) if scope.source else '', 'filter_account': scope.account_id, 'filter_service': params.get('service', ''),
            'currencies': sorted(set(Cost.objects.values_list('currency', flat=True).distinct()) | {'USD'}),
            'total': total, 'mtd': mtd, 'mtd_end': today - timedelta(days=1), 'mtd_complete': mtd_complete, 'previous': previous_total, 'delta': delta, 'percent': delta / abs(previous_total) * 100 if delta is not None and previous_total else None,
            'forecast': forecast, 'current_complete': current_complete, 'comparable': comparable, 'dimensions': dimensions, 'freshness': freshness,
            'stale_count': sum(row['state'] != 'Fresh' or not row['mtd_complete'] for row in freshness), 'region_url': '/?' + urlencode({k: v for k,v in params.items() if k in ('customer', 'source', 'account', 'currency', 'metric')} | {'start':str(start), 'end':str(today), 'group_by':'region'})}
