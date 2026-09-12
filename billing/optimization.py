"""Read-only, authorization-scoped budget controls and cost review opportunities."""
from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import urlencode
from dateutil.relativedelta import relativedelta
from django.db.models import Sum, Max, Count, Q
from django.utils import timezone
from . import budgets, scope
from .models import AccountAssignment, Alert, Budget, BudgetEvaluation, CollectionPeriod, Cost


def options(params):
    today = timezone.localdate()
    raw = params.get('month', today.strftime('%Y-%m'))
    try:
        month = date.fromisoformat(raw + '-01')
    except (ValueError, TypeError):
        raise ValueError('Choose a billing month in YYYY-MM format.') from None
    if month > today.replace(day=1):
        raise ValueError('Choose the current month or an earlier month.')
    currency = params.get('currency', 'USD').upper()
    if len(currency) != 3 or not currency.isalpha():
        raise ValueError('Choose a three-letter currency code.')
    metric = params.get('metric', 'unblended')
    if metric not in ('unblended', 'amortized'):
        raise ValueError('Choose unblended or amortized cost.')
    selected = scope.resolve(params)
    if selected.project:
        raise ValueError('Select a customer or account rather than a project for cost optimization.')
    return month, currency, metric, selected


def review_action(service):
    name = service.lower()
    if 'ec2' in name or 'elastic compute' in name:
        return 'Compute utilization review', 'Check instance CPU, memory and network utilization in Compute Optimizer. Confirm workload requirements before rightsizing or scheduling shutdowns; evaluate Savings Plans only after reviewing stable usage and existing coverage.'
    if 'relational database' in name or 'rds' in name:
        return 'Database capacity review', 'Review database CPU, connections, storage and availability requirements before changing instance sizes or evaluating reserved capacity.'
    if 's3' in name or 'simple storage' in name:
        return 'Storage lifecycle review', 'Review access frequency, object age and retention requirements before applying lifecycle or storage-class changes.'
    if 'cloudwatch' in name:
        return 'Observability retention review', 'Review log ingestion, retention periods and custom metrics; confirm operational and audit requirements before reducing retention.'
    return 'Service cost review', 'Open the account cost breakdown and inspect usage drivers, business ownership and resource utilization before making changes.'


def report(month, currency, metric, selected, show_budgets=True, today=None):
    today = today or timezone.localdate()
    end = min(month + relativedelta(months=1), today + timedelta(days=1))
    facts = selected.costs().filter(day__gte=month, day__lt=end, currency=currency)
    opportunities = []
    groups = facts.values('customer_id', 'customer__name', 'account_id', 'service').annotate(
        spend=Sum(metric), last_day=Max('day'), observed_days=Count('day', distinct=True)).filter(spend__gt=0).order_by('-spend', 'account_id', 'service')[:30]
    for item in groups:
        title, action = review_action(item['service'])
        item.update(title=title, action=action, savings=None, explorer_query=urlencode({
            'customer': item['customer_id'], 'account': item['account_id'], 'start': month.isoformat(),
            'end': (end - timedelta(days=1)).isoformat(), 'currency': currency, 'metric': metric,
            'service': item['service']}))
        opportunities.append(item)
    internal = [c for c in selected.customers if c.name.strip().casefold() == 'flentas'] if show_budgets else []
    configured = Budget.objects.filter(customer__in=internal, active=True, currency=currency, metric=metric)
    if selected.account_id:
        configured = configured.filter(scope=Budget.ACCOUNT, account_id=selected.account_id)
    if selected.source:
        # Source views must not reveal whole-customer budgets or unrelated accounts.
        configured = configured.filter(scope=Budget.SOURCE, source=selected.source)
    configured = list(configured.select_related('customer', 'source', 'project').prefetch_related('amounts'))
    rows = []
    now = timezone.now()
    for budget in configured:
        amount = budget.amount_for(month)
        rows_in_scope = budgets.scope_costs(budget, month, end)
        actual = rows_in_scope.aggregate(value=Sum(metric))['value']
        data_state = budgets.data_status(budget, month, now)
        if data_state == 'complete':
            periods = list(CollectionPeriod.objects.filter(source__in=budgets.scope_sources(budget), month=month))
            required_end = min(end - timedelta(days=1), today - timedelta(days=1))
            if not periods or any(not p.first_day or p.first_day > month or not p.last_day or p.last_day < required_end for p in periods):
                data_state = 'partial'
        completed = rows_in_scope.filter(day__lt=today).aggregate(value=Sum(metric))['value']
        forecast = None
        method = 'unavailable'
        if month == today.replace(day=1) and data_state == 'complete':
            forecast = budgets.run_rate(completed, (today - month).days, month)
            if forecast is not None:
                method = 'run_rate'
        status = 'Not configured' if amount is None else 'No data' if actual is None else 'Unverified'
        if amount is not None and actual is not None:
            if actual > amount:
                status = 'Over budget'
            elif forecast is not None and forecast > amount:
                status = 'Forecast over budget'
            elif actual >= amount * budget.actual_threshold / Decimal(100):
                status = 'At risk'
            elif data_state == 'complete':
                status = 'Within budget'
        evaluation = BudgetEvaluation(budget=budget, month=month, amount=amount, actual=actual,
            forecast=forecast, forecast_method=method, status=status, data_status=data_state)
        rows.append({'budget': budget, 'evaluation': evaluation})
    rows.sort(key=lambda row: (budgets.STATUS_ORDER.index(row['evaluation'].status), row['budget'].name))
    configured_accounts = {b.account_id for b in configured if b.scope == Budget.ACCOUNT and b.amount_for(month) is not None}
    assignments = AccountAssignment.objects.filter(customer__in=internal, start__lt=end).filter(
        Q(end__isnull=True) | Q(end__gt=month))
    if selected.account_id:
        assignments = assignments.filter(account__account_id=selected.account_id)
    if selected.source:
        assignments = assignments.none()
    missing = list(assignments.exclude(account__account_id__in=configured_accounts).values(
        'customer_id', 'customer__name', 'account__account_id').distinct().order_by('account__account_id'))
    return {'month': month, 'period_end': end - timedelta(days=1), 'currency': currency, 'metric': metric,
        'selected': selected, 'opportunities': opportunities, 'budget_rows': rows, 'missing_budgets': missing,
        'show_budgets': bool(internal), 'alerts': Alert.objects.filter(budget__in=configured, month=month).select_related('budget')[:50],
        'breach_count': sum(r['evaluation'].status in ('Over budget', 'Forecast over budget') for r in rows),
        'unverified_count': sum(r['evaluation'].data_status != 'complete' for r in rows)}
