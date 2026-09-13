"""Read-only, authorization-scoped budget controls and cost review opportunities."""
from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import urlencode
from dateutil.relativedelta import relativedelta
from django.db.models import Sum, Max, Count, Q
from django.utils import timezone
from django.urls import reverse
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


def owner_details(customer, account_id='', explicit='', assignments=()):
    """Use recorded ownership only; a billing contact is not an alert recipient."""
    owner, origin = explicit.strip(), 'Budget owner'
    if not owner and account_id:
        assignment = next((a for a in assignments if a.customer_id == customer.pk and a.account.account_id == account_id), None)
        owner = str((assignment.metadata or {}).get('owner', '')).strip() if assignment else ''
        origin = 'Account owner'
    if not owner and not account_id:
        owner, origin = customer.owner.strip(), 'Customer owner'
    return {'owner': owner or 'Unassigned', 'owner_origin': origin if owner else '',
            'owner_assigned': bool(owner),
            'owner_url': (reverse('account_detail', args=[account_id]) + '?' + urlencode({'customer': customer.pk})) if account_id else reverse('customer_detail', args=[customer.pk])}


def imported_controls(customers, month, currency, metric, selected, assignments):
    from .aws_budget_display import overview, account_snapshots, _charge_type_only
    imported = overview(customers)
    mapped = account_snapshots(customers, month, currency, metric)
    account_by_pk = {b.pk: key for key, values in mapped.items() for b in values}
    rows = []
    covered = set()
    for b in imported['aws_budgets']:
        if b.imported_at.date().replace(day=1) != month or b.limit_unit != currency or b.budget_type != 'COST' or b.time_unit != 'MONTHLY':
            continue
        metrics, types = b.raw.get('Metrics'), b.raw.get('CostTypes') or {}
        expected = 'AmortizedCost' if metric == 'amortized' else 'UnblendedCost'
        if (metrics and set(metrics) != {expected}) or (not metrics and (types.get('UseBlended') or bool(types.get('UseAmortized')) != (metric == 'amortized'))):
            continue
        account_key = account_by_pk.get(b.pk)
        if selected.account_id and (not account_key or account_key[1] != selected.account_id):
            continue
        if selected.source and b.source_id != selected.source.pk:
            continue
        amount = b.limit_amount
        actual = b.actual_amount if b.actual_unit == currency else None
        forecast = b.forecast_amount if b.forecast_unit == currency else None
        remaining_filters = dict(b.filters or {})
        remaining_filters.pop('LinkedAccount', None)
        if remaining_filters.get('Dimensions', {}).get('Key') in ('LINKED_ACCOUNT', 'LinkedAccount'):
            remaining_filters.pop('Dimensions')
        account_wide = _charge_type_only(remaining_filters)
        if account_key and account_wide and amount is not None and amount > 0:
            covered.add(account_key)
        account_id = account_key[1] if account_key else ''
        rows.append({'snapshot': b, 'account_id': account_id, 'account_wide': account_wide,
            'actual_percent': actual / amount * 100 if actual is not None and amount else None,
            'actual_overrun': max(actual - amount, Decimal(0)) if actual is not None and amount is not None else None,
            'forecast_overrun': max(forecast - amount, Decimal(0)) if forecast is not None and amount is not None else None,
            'forecast_percent': forecast / amount * 100 if forecast is not None and amount else None,
            **owner_details(b.source.customer, account_id, assignments=assignments)})
    connections = imported['aws_budget_connections']
    if selected.source:
        connections = [r for r in connections if r['source'].pk == selected.source.pk]
    if selected.account_id:
        connections = [r for r in connections if r['source'].account_id == selected.account_id or any(a.account.source_id == r['source'].pk and a.account.account_id == selected.account_id for a in assignments)]
    rows.sort(key=lambda r: (-(r['actual_overrun'] or Decimal(0)), -(r['forecast_overrun'] or Decimal(0)), r['snapshot'].name))
    return rows, covered, connections


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
    owner_assignments = list(AccountAssignment.objects.filter(customer__in=internal, start__lte=today).filter(Q(end__isnull=True) | Q(end__gt=today)).select_related('account'))
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
        rows.append({'budget': budget, 'evaluation': evaluation,
            'forecast_overrun': max(forecast - amount, Decimal(0)) if forecast is not None and amount is not None else None,
            'forecast_threshold_reached': forecast is not None and amount is not None and forecast >= amount * budget.forecast_threshold / Decimal(100),
            **owner_details(budget.customer, budget.account_id if budget.scope == Budget.ACCOUNT else '', budget.owner, owner_assignments)})
    rows.sort(key=lambda row: (budgets.STATUS_ORDER.index(row['evaluation'].status), row['budget'].name))
    imported_rows, imported_accounts, imported_connections = imported_controls(internal, month, currency, metric, selected, owner_assignments)
    configured_accounts = {(b.customer_id, b.account_id) for b in configured if b.scope == Budget.ACCOUNT and b.amount_for(month) is not None} | imported_accounts
    assignments = AccountAssignment.objects.filter(customer__in=internal, start__lt=end).filter(
        Q(end__isnull=True) | Q(end__gt=month))
    if selected.account_id:
        assignments = assignments.filter(account__account_id=selected.account_id)
    if selected.source:
        assignments = assignments.none()
    missing = [r for r in assignments.values('customer_id', 'customer__name', 'account__account_id').distinct().order_by('account__account_id')
               if (r['customer_id'], r['account__account_id']) not in configured_accounts]
    owner_by_budget = {r['budget'].pk: r for r in rows}
    alerts = list(Alert.objects.filter(budget__in=configured, month=month).select_related('budget')[:50])
    for alert in alerts:
        alert.responsible_owner = owner_by_budget[alert.budget_id]['owner']
        alert.owner_origin = owner_by_budget[alert.budget_id]['owner_origin']
    imported = {'imported_rows': imported_rows[:20], 'imported_total': len(imported_rows), 'aws_budget_connections': imported_connections}
    return {**imported, 'month': month, 'period_end': end - timedelta(days=1), 'currency': currency, 'metric': metric,
        'selected': selected, 'opportunities': opportunities, 'budget_rows': rows, 'missing_budgets': missing,
        'show_budgets': bool(internal), 'alerts': alerts,
        'breach_count': sum(r['evaluation'].status in ('Over budget', 'Forecast over budget') for r in rows),
        'unverified_count': sum(r['evaluation'].data_status != 'complete' for r in rows)}
