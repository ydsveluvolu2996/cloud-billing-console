"""Budget evaluation for customer, payer, account and project scopes.

Dashboard budgets are evaluated against stored facts with matching scope, month, metric and
currency. Forecasts prefer cached AWS forecasts and fall back to a clearly labelled completed-day
run-rate projection; insufficient history or coverage yields "Unavailable". Incomplete or stale
data never produces a "Within budget" verdict.
"""
import csv
import io
from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone
from . import scope as scoping
from .models import (Alert, BillingSource, Budget, BudgetAmount, BudgetEvaluation, CollectionPeriod, Cost, Customer, Project, ProjectCost)
from .query_cache import get_query

FORECAST_METRICS = {'unblended': 'UNBLENDED_COST', 'amortized': 'AMORTIZED_COST'}
STATUS_ORDER = ['Over budget', 'Forecast over budget', 'At risk', 'Unverified', 'No data', 'Within budget', 'Not configured']


def month_of(day):
    return day.replace(day=1)


def apply_filters(qs, filters):
    if filters.get('include_services'):
        qs = qs.filter(service__in=filters['include_services'])
    if filters.get('exclude_services'):
        qs = qs.exclude(service__in=filters['exclude_services'])
    return qs


def scope_costs(budget, first, last):
    """Cost rows for the budget scope between first (inclusive) and last (exclusive)."""
    if budget.scope == Budget.PROJECT:
        return ProjectCost.objects.filter(project=budget.project, currency=budget.currency, day__gte=first, day__lt=last)
    qs = Cost.objects.filter(customer=budget.customer, currency=budget.currency, day__gte=first, day__lt=last)
    if budget.scope == Budget.SOURCE:
        qs = qs.filter(source=budget.source)
    elif budget.scope == Budget.ACCOUNT:
        qs = qs.filter(account_id=budget.account_id)
    return apply_filters(qs, budget.filters or {})


def scope_sources(budget):
    if budget.scope == Budget.SOURCE:
        return [budget.source]
    if budget.scope == Budget.ACCOUNT:
        return list(BillingSource.objects.filter(Q(accounts__account_id=budget.account_id) | Q(costs__account_id=budget.account_id)).distinct())
    return [s for s, _, _ in scoping.report_units(budget.customer, active_only=False) if s.enabled]


def data_status(budget, month, now=None):
    """complete | partial | stale | missing for the budget's sources in ``month``."""
    now = now or timezone.now()
    sources = scope_sources(budget)
    if not sources:
        return 'missing'
    periods = {p.source_id: p for p in CollectionPeriod.objects.filter(source__in=sources, month=month)}
    if not periods:
        return 'missing'
    if any(p.status in ('failed', 'partial') for p in periods.values()) or len(periods) < len(sources):
        return 'partial'
    if any(not p.last_success or p.last_success < now - BillingSource.STALE_AFTER for p in periods.values()) and month == month_of(now.date()):
        return 'stale'
    return 'complete'


def aws_forecast(budget, month, today):
    """Sum of cached AWS forecasts for the scope, or None when any part is unavailable."""
    if not getattr(settings, 'BUDGET_AWS_FORECASTS', True) or budget.scope == Budget.PROJECT:
        return None
    month_end = month + relativedelta(months=1)
    if today >= month_end or today < month:
        return None
    total = Decimal(0)
    for source, _, accounts in scoping.report_units(budget.customer):
        if budget.scope == Budget.SOURCE and source.pk != budget.source_id:
            continue
        if budget.scope == Budget.ACCOUNT:
            if accounts is not None and budget.account_id not in accounts:
                continue
            accounts = [budget.account_id]
        parts = []
        if accounts is not None:
            parts.append({'Dimensions': {'Key': 'LINKED_ACCOUNT', 'Values': sorted(accounts)}})
        for key, mode in (('include_services', 'include'), ('exclude_services', 'exclude')):
            if (budget.filters or {}).get(key):
                expr = {'Dimensions': {'Key': 'SERVICE', 'Values': budget.filters[key]}}
                parts.append(expr if mode == 'include' else {'Not': expr})
        request = {'TimePeriod': {'Start': today.isoformat(), 'End': month_end.isoformat()}, 'Granularity': 'MONTHLY',
                   'Metric': FORECAST_METRICS[budget.metric], 'PredictionIntervalLevel': 80}
        if len(parts) == 1:
            request['Filter'] = parts[0]
        elif parts:
            request['Filter'] = {'And': parts}
        query = get_query(source, 'get_cost_forecast', request, customer=budget.customer)
        if not query.data or not query.data.get('ForecastResultsByTime'):
            return None
        total += sum(Decimal(p['MeanValue']) for p in query.data['ForecastResultsByTime'])
    return total


def run_rate(actual_completed, completed_days, month):
    if completed_days < getattr(settings, 'RUN_RATE_MIN_DAYS', 3) or actual_completed is None:
        return None
    return actual_completed / completed_days * monthrange(month.year, month.month)[1]


def evaluate_budget(budget, month, today=None, persist=True, now=None):
    now = now or timezone.now()
    today = today or now.date()
    first, last = month, month + relativedelta(months=1)
    amount = budget.amount_for(month)
    status_data = data_status(budget, month, now)
    rows = scope_costs(budget, first, min(last, today + timedelta(days=1)))
    actual = rows.aggregate(v=Sum(budget.metric))['v']
    completed = rows.filter(day__lt=today).aggregate(v=Sum(budget.metric))['v'] if month == month_of(today) else actual
    completed_days = (today - first).days if month == month_of(today) else monthrange(month.year, month.month)[1]
    forecast, method = None, 'unavailable'
    if month == month_of(today):
        aws = aws_forecast(budget, month, today) if status_data != 'missing' else None
        if aws is not None:
            forecast, method = (completed or Decimal(0)) + aws, 'aws'
        elif status_data in ('complete', 'stale'):
            projection = run_rate(completed, completed_days, month)
            if projection is not None:
                forecast, method = projection, 'run_rate'
    elif actual is not None:
        forecast, method = actual, 'actual'
    if amount is None:
        status = 'Not configured'
    elif actual is None or status_data == 'missing':
        status = 'No data'
    elif actual > amount:
        status = 'Over budget'
    elif forecast is not None and forecast > amount * budget.forecast_threshold / 100:
        status = 'Forecast over budget'
    elif actual >= amount * budget.actual_threshold / 100:
        status = 'At risk'
    elif status_data != 'complete':
        status = 'Unverified'
    else:
        status = 'Within budget'
    detail = {'completed_days': completed_days, 'data_status': status_data, 'forecast_method': method, 'source': 'dashboard'}
    evaluation = BudgetEvaluation(budget=budget, month=month, evaluated_at=now, amount=amount, actual=actual, forecast=forecast,
                                  forecast_method=method, status=status, data_status=status_data, detail=detail)
    if persist:
        with transaction.atomic():
            BudgetEvaluation.objects.update_or_create(budget=budget, month=month, defaults={
                'evaluated_at': now, 'amount': amount, 'actual': actual, 'forecast': forecast, 'forecast_method': method,
                'status': status, 'data_status': status_data, 'detail': detail})
            raise_alerts(budget, month, amount, actual, forecast, now)
    return evaluation


def raise_alerts(budget, month, amount, actual, forecast, now):
    """Threshold alerts are deduplicated per budget/month/kind/threshold."""
    if amount is None:
        return
    if actual is not None and actual >= amount * budget.actual_threshold / 100:
        Alert.objects.get_or_create(budget=budget, month=month, kind='actual', threshold=budget.actual_threshold, defaults={
            'value': actual, 'triggered_at': now,
            'message': f'{budget.name}: actual spend {actual:.2f} {budget.currency} reached {budget.actual_threshold}% of {amount:.2f}.'})
    if forecast is not None and forecast >= amount * budget.forecast_threshold / 100:
        Alert.objects.get_or_create(budget=budget, month=month, kind='forecast', threshold=budget.forecast_threshold, defaults={
            'value': forecast, 'triggered_at': now,
            'message': f'{budget.name}: forecast {forecast:.2f} {budget.currency} reaches {budget.forecast_threshold}% of {amount:.2f}.'})


def evaluate_all(today=None, months=2):
    today = today or timezone.now().date()
    current = month_of(today)
    count = 0
    for budget in Budget.objects.filter(active=True, customer__active=True).select_related('customer', 'source', 'project').prefetch_related('amounts'):
        for offset in range(months):
            try:
                evaluate_budget(budget, current - relativedelta(months=offset), today=today)
                count += 1
            except Exception:
                continue
    return count


def latest_evaluations(budgets, month):
    return {e.budget_id: e for e in BudgetEvaluation.objects.filter(budget__in=budgets, month=month)}


def portfolio_summary(month, evaluations=None):
    """Customer-level budgets only: child budgets are shown separately, never added to parents."""
    budgets = list(Budget.objects.filter(active=True, customer__active=True).select_related('customer'))
    evaluations = evaluations if evaluations is not None else latest_evaluations(budgets, month)
    summary = {'customer_budget_total': {}, 'customer_actual_total': {}, 'breaches': 0, 'at_risk': 0, 'unverified': 0, 'child_budgets': 0, 'customer_budgets': 0}
    for budget in budgets:
        evaluation = evaluations.get(budget.pk)
        if budget.scope == Budget.CUSTOMER:
            summary['customer_budgets'] += 1
            if evaluation and evaluation.amount is not None:
                summary['customer_budget_total'][budget.currency] = summary['customer_budget_total'].get(budget.currency, Decimal(0)) + evaluation.amount
                if evaluation.actual is not None:
                    summary['customer_actual_total'][budget.currency] = summary['customer_actual_total'].get(budget.currency, Decimal(0)) + evaluation.actual
        else:
            summary['child_budgets'] += 1
        if evaluation:
            if evaluation.status == 'Over budget':
                summary['breaches'] += 1
            elif evaluation.status in ('Forecast over budget', 'At risk'):
                summary['at_risk'] += 1
            elif evaluation.status in ('Unverified', 'No data'):
                summary['unverified'] += 1
    return summary


def child_allocation_check(customer, month, evaluations=None):
    """Compare child (account/project/payer) budget totals with the customer budget without summing them into it."""
    budgets = list(customer.budgets.filter(active=True).prefetch_related('amounts'))
    parent = next((b for b in budgets if b.scope == Budget.CUSTOMER), None)
    parent_amount = parent.amount_for(month) if parent else None
    children = {}
    for b in budgets:
        if b.scope != Budget.CUSTOMER:
            amount = b.amount_for(month)
            if amount is not None:
                children.setdefault((b.scope, b.currency), Decimal(0))
                children[(b.scope, b.currency)] += amount
    rows = [{'scope': scope, 'currency': currency, 'total': total,
             'exceeds_parent': parent_amount is not None and parent and currency == parent.currency and total > parent_amount}
            for (scope, currency), total in sorted(children.items())]
    return {'parent': parent, 'parent_amount': parent_amount, 'children': rows}


# --- bulk CSV -----------------------------------------------------------------------------------

BUDGET_COLUMNS = ['customer', 'scope', 'name', 'amount', 'currency', 'metric', 'payer_account', 'account_id', 'project', 'effective_from', 'actual_threshold', 'forecast_threshold']


def parse_budget_csv(text):
    """Validate a budget CSV; returns (rows, errors). Nothing is written."""
    reader = csv.DictReader(io.StringIO(text))
    rows, errors = [], []
    missing = [c for c in ('customer', 'scope', 'name', 'amount') if c not in (reader.fieldnames or [])]
    if missing:
        return [], [f'Missing required column(s): {", ".join(missing)}']
    customers = {c.name.lower(): c for c in Customer.objects.all()}
    for number, raw in enumerate(reader, start=2):
        row = {k: (v or '').strip() for k, v in raw.items() if k}
        problems = []
        customer = customers.get(row.get('customer', '').lower())
        if customer is None:
            problems.append('unknown customer')
        scope = row.get('scope', 'customer').lower() or 'customer'
        if scope not in dict(Budget.SCOPES):
            problems.append('scope must be customer, source, account or project')
        try:
            amount = Decimal(row.get('amount', ''))
            if amount <= 0:
                problems.append('amount must be positive')
        except InvalidOperation:
            amount = None
            problems.append('amount is not a number')
        currency = (row.get('currency') or (customer.currency if customer else 'USD')).upper()
        if len(currency) != 3 or not currency.isalpha():
            problems.append('currency must be a 3-letter code')
        metric = (row.get('metric') or 'unblended').lower()
        if metric not in ('unblended', 'amortized'):
            problems.append('metric must be unblended or amortized')
        source = project = None
        if scope == 'source':
            source = BillingSource.objects.filter(account_id=row.get('payer_account', ''), customer=customer).first() if customer else None
            if source is None:
                problems.append('payer_account is not a connection of this customer')
        if scope == 'account':
            if not customer or not customer.assignments.filter(account__account_id=row.get('account_id', '')).exists():
                problems.append('account_id is not assigned to this customer')
        if scope == 'project':
            project = Project.objects.filter(customer=customer, name=row.get('project', '')).first() if customer else None
            if project is None:
                problems.append('project not found for this customer')
        try:
            effective_from = date.fromisoformat(row['effective_from']).replace(day=1) if row.get('effective_from') else date.today().replace(day=1)
        except ValueError:
            effective_from = None
            problems.append('effective_from must be YYYY-MM-DD')
        thresholds = {}
        for key, default in (('actual_threshold', Decimal('80')), ('forecast_threshold', Decimal('100'))):
            try:
                thresholds[key] = Decimal(row[key]) if row.get(key) else default
            except InvalidOperation:
                problems.append(f'{key} is not a number')
        if not row.get('name'):
            problems.append('name is required')
        existing = Budget.objects.filter(customer=customer, scope=scope, name=row.get('name', '')).first() if customer else None
        rows.append({'line': number, 'customer': customer.name if customer else row.get('customer', ''), 'customer_id': str(customer.pk) if customer else '',
                     'scope': scope, 'name': row.get('name', ''), 'amount': str(amount) if amount is not None else row.get('amount', ''),
                     'currency': currency, 'metric': metric, 'source_id': str(source.pk) if source else '', 'account_id': row.get('account_id', ''),
                     'project_id': project.pk if project else None, 'effective_from': effective_from.isoformat() if effective_from else '',
                     'actual_threshold': str(thresholds.get('actual_threshold', '')), 'forecast_threshold': str(thresholds.get('forecast_threshold', '')),
                     'action': 'update' if existing else 'create', 'problems': problems})
        errors.extend(f'Line {number}: {p}' for p in problems)
    if not rows:
        errors.append('The file contains no budget rows.')
    return rows, errors


def apply_budget_rows(rows, actor):
    created = updated = 0
    with transaction.atomic():
        for row in rows:
            if row['problems']:
                raise ValueError(f'Line {row["line"]} has validation problems.')
            customer = Customer.objects.get(pk=row['customer_id'])
            budget, made = Budget.objects.get_or_create(customer=customer, scope=row['scope'], name=row['name'], defaults={
                'currency': row['currency'], 'metric': row['metric'], 'source_id': row['source_id'] or None,
                'account_id': row['account_id'], 'project_id': row['project_id'], 'created_by': actor,
                'actual_threshold': Decimal(row['actual_threshold']), 'forecast_threshold': Decimal(row['forecast_threshold'])})
            if not made:
                budget.currency, budget.metric = row['currency'], row['metric']
                budget.actual_threshold, budget.forecast_threshold = Decimal(row['actual_threshold']), Decimal(row['forecast_threshold'])
                budget.save()
            set_recurring_amount(budget, Decimal(row['amount']), date.fromisoformat(row['effective_from']))
            created += made
            updated += not made
    return created, updated


def set_recurring_amount(budget, amount, effective_from):
    """Close the current recurring amount at ``effective_from`` and start a new one (history preserved)."""
    effective_from = effective_from.replace(day=1)
    for current in budget.amounts.filter(month__isnull=True, effective_to__isnull=True):
        if current.effective_from >= effective_from:
            current.delete()
        else:
            current.effective_to = effective_from
            current.save(update_fields=['effective_to'])
    return BudgetAmount.objects.create(budget=budget, amount=amount, effective_from=effective_from)


def set_override(budget, month, amount):
    month = month.replace(day=1)
    override, _ = BudgetAmount.objects.update_or_create(budget=budget, month=month, defaults={'amount': amount, 'effective_from': month})
    return override
