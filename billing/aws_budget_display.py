"""Map AWS snapshots only when their filters identify an authorized account."""
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from .models import ImportedBudget, AccountAssignment, BillingSource


def _charge_type_only(filters):
    """Credit/refund exclusions still describe an account-wide cost budget."""
    if not filters:
        return True
    if set(filters) == {'RecordType'}:
        return True
    if set(filters) == {'Dimensions'}:
        return filters['Dimensions'].get('Key') == 'RECORD_TYPE'
    if set(filters) == {'Not'}:
        return _charge_type_only(filters['Not'])
    for operator in ('And', 'Or'):
        if set(filters) == {operator}:
            return all(_charge_type_only(child) for child in filters[operator])
    return False


def account_snapshots(customers, month, currency, metric='unblended'):
    customers = list(customers)
    owned = {(r['customer_id'], r['account__account_id']) for r in AccountAssignment.objects.filter(customer__in=customers, end__isnull=True).values('customer_id', 'account__account_id')}
    result = {}
    for budget in ImportedBudget.objects.filter(source__customer__in=customers, budget_type='COST', time_unit='MONTHLY', limit_unit=currency).select_related('source'):
        # AWS calculated spend and limits are snapshots, not historical evaluations.
        if budget.imported_at.date().replace(day=1) != month:
            continue
        start = parse_datetime(str(budget.time_period.get('Start', '')))
        end = parse_datetime(str(budget.time_period.get('End', '')))
        if (start and start.date() > timezone.now().date()) or (end and end.date() <= timezone.now().date()):
            continue
        metrics = budget.raw.get('Metrics')
        expected_metric = 'AmortizedCost' if metric == 'amortized' else 'UnblendedCost'
        if metrics and set(metrics) != {expected_metric}:
            continue
        cost_types = budget.raw.get('CostTypes') or {}
        if not metrics and (cost_types.get('UseBlended') or bool(cost_types.get('UseAmortized')) != (metric == 'amortized')):
            continue
        filters = budget.filters or {}
        linked = filters.get('LinkedAccount')
        dimensions = filters.get('Dimensions', {})
        if dimensions.get('Key') in ('LINKED_ACCOUNT', 'LinkedAccount'):
            linked = dimensions.get('Values')
        if linked is not None:
            account = linked[0] if isinstance(linked, list) and len(linked) == 1 else None
        elif budget.source.kind in (BillingSource.STANDALONE, BillingSource.MEMBER_BUDGETS) and _charge_type_only(filters):
            account = budget.owning_account_id
        else:
            # Never assign an organization-wide payer budget to payer-own spend.
            account = None
        if not account or (budget.source.customer_id, account) not in owned:
            continue
        budget.snapshot_stale = bool(budget.source.capabilities.get('budgets_error')) or timezone.now() - budget.imported_at > BillingSource.STALE_AFTER
        result.setdefault((budget.source.customer_id, account), []).append(budget)
    return result


def overview(customers):
    customers = list(customers)
    snapshots = list(ImportedBudget.objects.filter(source__customer__in=customers).select_related('source'))
    for budget in snapshots:
        budget.snapshot_stale = bool(budget.source.capabilities.get('budgets_error')) or timezone.now() - budget.imported_at > BillingSource.STALE_AFTER
    connections = []
    for source in BillingSource.objects.filter(customer__in=customers, enabled=True):
        if 'budgets' not in source.approved_capabilities:
            state = 'Budget import approval needed'
        elif source.capabilities.get('budgets_error'):
            state = 'Budget read unavailable — verify IAM permission'
        elif source.capabilities.get('budgets'):
            state = 'Automatic import enabled'
        else:
            state = 'Waiting for budget permission check'
        connections.append({'source': source, 'state': state})
    return {'aws_budgets': snapshots, 'aws_budget_connections': connections}
