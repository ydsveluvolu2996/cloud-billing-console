"""Project allocation: versioned, effective-dated, mutually exclusive rules.

Account-based rules are computed exactly from stored facts. Tag and cost-category rules use
scoped, cached AWS queries (grouped by LINKED_ACCOUNT, daily) because the additive facts only
carry account and service. Overlapping rules are rejected at save time; whole-account claims are
excluded from tag/category queries so no dollar is allocated twice. The remainder stays visible
as "Unallocated / shared" and reconciles with customer spend.
"""
from datetime import date, timedelta
from decimal import Decimal
from dateutil.relativedelta import relativedelta
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from . import scope as scoping
from .models import AllocationRule, BillingSource, Cost, Project, ProjectCost
from .query_cache import get_query

METRIC_KEYS = {'unblended': 'UnblendedCost', 'amortized': 'AmortizedCost'}


def overlapping_dates(a, b):
    return a.effective_start < (b.effective_end or date.max) and b.effective_start < (a.effective_end or date.max)


def customer_rules(customer, exclude_pk=None):
    rules = AllocationRule.objects.filter(project__customer=customer, active=True).select_related('project')
    if exclude_pk:
        rules = rules.exclude(pk=exclude_pk)
    return list(rules)


def validate_rule(rule, customer):
    """Raise ValueError when ``rule`` could allocate the same spend as an existing rule."""
    if rule.uses_accounts:
        owned = set(scoping.customer_accounts(customer)) | set(scoping.customer_accounts(customer, on=rule.effective_start))
        foreign = sorted(set(rule.account_ids) - owned)
        if foreign:
            raise ValueError(f'Accounts {", ".join(foreign)} are not assigned to {customer.name}.')
    for other in customer_rules(customer, exclude_pk=rule.pk):
        if not overlapping_dates(rule, other):
            continue
        shared_accounts = set(rule.account_ids) & set(other.account_ids)
        same_scope = (not rule.uses_accounts and not other.uses_accounts) or shared_accounts \
            or (rule.uses_accounts != other.uses_accounts and not (rule.kind == AllocationRule.ACCOUNTS or other.kind == AllocationRule.ACCOUNTS))
        if rule.kind == AllocationRule.ACCOUNTS and other.kind == AllocationRule.ACCOUNTS:
            if shared_accounts:
                raise ValueError(f'Accounts {", ".join(sorted(shared_accounts))} already belong to project {other.project.name}.')
            continue
        if rule.kind == AllocationRule.ACCOUNTS or other.kind == AllocationRule.ACCOUNTS:
            whole, partial = (rule, other) if rule.kind == AllocationRule.ACCOUNTS else (other, rule)
            if partial.uses_accounts and set(whole.account_ids) & set(partial.account_ids):
                raise ValueError(f'Accounts {", ".join(sorted(set(whole.account_ids) & set(partial.account_ids)))} are fully claimed by project {whole.project.name}; remove them from the tag/category scope.')
            continue  # customer-wide tag rules exclude whole-account claims exactly
        if not same_scope:
            continue
        if rule.key == other.key and set(rule.values) & set(other.values):
            raise ValueError(f'Value(s) {", ".join(sorted(set(rule.values) & set(other.values)))} of {rule.key} already allocate to project {other.project.name}.')
        if rule.key != other.key:
            raise ValueError(f'Rule overlaps project {other.project.name}, which uses a different key ({other.key}). Use the same key with disjoint values or disjoint account scopes.')
    return True


def claimed_accounts(customer, day, exclude_rule=None):
    """Accounts fully claimed by ACCOUNTS rules on ``day``."""
    claimed = set()
    for rule in customer_rules(customer):
        if rule.kind == AllocationRule.ACCOUNTS and rule.covers(day) and (exclude_rule is None or rule.pk != exclude_rule.pk):
            claimed |= set(rule.account_ids)
    return claimed


def rule_scope_accounts(rule, customer, source, day):
    """Accounts a tag/category rule applies to inside one source on ``day`` (exact, disjoint)."""
    owned = set(scoping.customer_accounts(customer, on=day, source=source))
    if rule.uses_accounts:
        owned &= set(rule.account_ids)
    return sorted(owned - claimed_accounts(customer, day, exclude_rule=rule))


def aws_request(rule, accounts, start, end):
    key = 'Tags' if rule.kind in (AllocationRule.TAG, AllocationRule.ACCOUNT_TAG) else 'CostCategories'
    expression = {'And': [{key: {'Key': rule.key, 'Values': list(rule.values)}},
                          {'Dimensions': {'Key': 'LINKED_ACCOUNT', 'Values': accounts}}]}
    return {'TimePeriod': {'Start': start.isoformat(), 'End': end.isoformat()}, 'Granularity': 'DAILY',
            'Metrics': ['UnblendedCost', 'AmortizedCost'], 'Filter': expression,
            'GroupBy': [{'Type': 'DIMENSION', 'Key': 'LINKED_ACCOUNT'}]}


def rule_months(rule, months):
    for month in months:
        first, last = month, month + relativedelta(months=1)
        if rule.effective_start < last and (rule.effective_end is None or rule.effective_end > first):
            yield month, max(first, rule.effective_start), min(last, rule.effective_end or last)


def allocate_accounts_rule(rule, customer, month, first, last):
    """Exact allocation from additive facts for whole-account rules."""
    rows = (Cost.objects.filter(customer=customer, account_id__in=rule.account_ids, day__gte=first, day__lt=last)
            .values('source_id', 'day', 'account_id', 'currency').annotate(u=Sum('unblended'), a=Sum('amortized')))
    estimated_days = set(Cost.objects.filter(customer=customer, account_id__in=rule.account_ids, day__gte=first, day__lt=last, estimated=True).values_list('day', flat=True))
    return [ProjectCost(project=rule.project, rule=rule, source_id=r['source_id'], day=r['day'], account_id=r['account_id'], currency=r['currency'],
                        unblended=r['u'], amortized=r['a'], estimated=r['day'] in estimated_days) for r in rows], True


def allocate_query_rule(rule, customer, month, first, last, today):
    """Allocation from scoped cached AWS queries; returns (rows, complete)."""
    rows, complete = [], True
    end = min(last, today + timedelta(days=1))
    if first >= end:
        return rows, True
    for source, _, _ in scoping.report_units(customer):
        accounts = rule_scope_accounts(rule, customer, source, first)
        if not accounts:
            continue
        query = get_query(source, 'get_cost_and_usage', aws_request(rule, accounts, first, end), customer=customer)
        if query.data is None:
            complete = False
            continue
        for period in query.data.get('ResultsByTime', []):
            day = date.fromisoformat(period['TimePeriod']['Start'])
            if not first <= day < end:
                raise ValueError('AWS returned a period outside the allocation window.')
            for group in period.get('Groups', []):
                u, a = group['Metrics']['UnblendedCost'], group['Metrics']['AmortizedCost']
                rows.append(ProjectCost(project=rule.project, rule=rule, source=source, day=day, account_id=group['Keys'][0],
                                        currency=u['Unit'], unblended=Decimal(u['Amount']), amortized=Decimal(a['Amount']),
                                        estimated=bool(period.get('Estimated', False))))
    return rows, complete


def allocate_project(project, months=None, today=None):
    """Recompute allocations for a project; each rule/month is replaced atomically."""
    today = today or timezone.now().date()
    months = months or [today.replace(day=1) - relativedelta(months=i) for i in range(2)]
    customer = project.customer
    status = {'complete': True, 'rows': 0}
    for rule in project.rules.filter(active=True):
        for month in months:  # drop allocations for days a (re)dated rule no longer covers
            month_end = month + relativedelta(months=1)
            stale = ProjectCost.objects.filter(rule=rule, day__gte=month, day__lt=month_end)
            if rule.effective_end and rule.effective_end < month_end:
                stale.filter(day__gte=rule.effective_end).delete()
            if rule.effective_start > month:
                stale.filter(day__lt=rule.effective_start).delete()
        for month, first, last in rule_months(rule, months):
            if rule.kind == AllocationRule.ACCOUNTS:
                rows, complete = allocate_accounts_rule(rule, customer, month, first, last)
            else:
                rows, complete = allocate_query_rule(rule, customer, month, first, last, today)
            status['complete'] = status['complete'] and complete
            if not complete and not rows:
                continue  # keep the previous snapshot until AWS data arrives
            with transaction.atomic():
                ProjectCost.objects.filter(rule=rule, day__gte=first, day__lt=last).delete()
                ProjectCost.objects.bulk_create(rows, batch_size=1000)
            status['rows'] += len(rows)
    ProjectCost.objects.filter(project=project).exclude(rule__active=True).delete()
    return status


def allocate_all(today=None):
    count = 0
    for project in Project.objects.filter(active=True, customer__active=True).select_related('customer'):
        try:
            allocate_project(project, today=today)
            count += 1
        except Exception:  # one customer's rule problem must not stop the others
            continue
    return count


def reconcile(customer, start, end, currency='USD', metric='unblended'):
    """Project allocations plus remainder versus customer spend for matching dates/currency/metric."""
    facts = Cost.objects.filter(customer=customer, currency=currency, day__gte=start, day__lte=end)
    total = facts.aggregate(v=Sum(metric))['v']
    projects = []
    allocated = Decimal(0)
    pending = False
    for project in customer.projects.filter(active=True).prefetch_related('rules'):
        qs = ProjectCost.objects.filter(project=project, currency=currency, day__gte=start, day__lte=end)
        amount = qs.aggregate(v=Sum(metric))['v']
        rules = [r for r in project.rules.all() if r.active]
        needs_aws = any(r.uses_aws_query for r in rules)
        has_data = amount is not None
        if needs_aws and not has_data and rules:
            pending = True
        allocated += amount or Decimal(0)
        projects.append({'project': project, 'amount': amount, 'rules': rules, 'accounts': qs.values('account_id').distinct().count(),
                         'share': float(amount / total * 100) if total and amount is not None else None, 'pending': needs_aws and not has_data})
    remainder = (total - allocated) if total is not None else None
    return {'total': total, 'allocated': allocated if projects else None, 'remainder': remainder, 'projects': projects,
            'pending': pending, 'reconciles': total is None or remainder is None or remainder >= Decimal('-0.0000000001'),
            'currency': currency, 'metric': metric, 'has_facts': total is not None}


def preview_rule(rule, customer, start, end, today=None):
    """Estimate what a rule would allocate; tag rules may be pending on AWS data."""
    today = today or timezone.now().date()
    validate_rule(rule, customer)
    if rule.kind == AllocationRule.ACCOUNTS:
        rows = Cost.objects.filter(customer=customer, account_id__in=rule.account_ids, day__gte=start, day__lte=end).values('currency').annotate(u=Sum('unblended'), a=Sum('amortized'))
        return {'pending': False, 'totals': {r['currency']: {'unblended': r['u'], 'amortized': r['a']} for r in rows}}
    totals, pending = {}, False
    for source, _, _ in scoping.report_units(customer):
        accounts = rule_scope_accounts(rule, customer, source, start)
        if not accounts:
            continue
        query = get_query(source, 'get_cost_and_usage', aws_request(rule, accounts, start, min(end + timedelta(days=1), today + timedelta(days=1))), customer=customer)
        if query.data is None:
            pending = True
            continue
        for period in query.data.get('ResultsByTime', []):
            for group in period.get('Groups', []):
                u, a = group['Metrics']['UnblendedCost'], group['Metrics']['AmortizedCost']
                bucket = totals.setdefault(u['Unit'], {'unblended': Decimal(0), 'amortized': Decimal(0)})
                bucket['unblended'] += Decimal(u['Amount'])
                bucket['amortized'] += Decimal(a['Amount'])
    return {'pending': pending, 'totals': totals}


def project_daily(project, start, end, currency, metric):
    rows = ProjectCost.objects.filter(project=project, currency=currency, day__gte=start, day__lte=end).values('day').annotate(v=Sum(metric)).order_by('day')
    return {r['day']: r['v'] for r in rows}


def project_accounts(project, start, end, currency, metric):
    return list(ProjectCost.objects.filter(project=project, currency=currency, day__gte=start, day__lte=end).values('account_id').annotate(v=Sum(metric)).order_by('-v'))
