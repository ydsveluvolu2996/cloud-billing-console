"""Atomic, repeatable Cost Explorer collection per billing source.

Baseline facts are DAILY costs grouped by LINKED_ACCOUNT and SERVICE. Each month is
published atomically; a failed month keeps its previous successful snapshot. Costs are
stamped with the customer that owned the linked account on that day.
"""
import json
import logging
from datetime import date, timedelta
from decimal import Decimal
from botocore.exceptions import ClientError
from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.utils import timezone
from .aws import Meter, RequestBudgetExceeded, Session, paginate
from .models import AccountAssignment, AwsAccount, BillingSource, CollectionPeriod, Cost, ImportedBudget, SyncRun

logger = logging.getLogger(__name__)
EPOCH = date(2000, 1, 1)


class OverlappingBillingScope(Exception):
    def __init__(self, account_id='', other=''):
        super().__init__(account_id)
        self.account_id, self.other = account_id, other


class ConnectionChanged(Exception):
    pass


def _check_publication_source(current, source):
    """Recheck authorization after AWS returns, while the source row is locked."""
    fields = ('connection_version', 'customer_id', 'account_id', 'role_arn', 'external_id',
              'kind', 'shared', 'approved_capabilities')
    if (not current.enabled or not current.customer.active
            or any(getattr(current, field) != getattr(source, field) for field in fields)):
        raise ConnectionChanged()
    if settings.REQUIRE_CONNECTION_APPROVAL:
        from .iam import approval_ready
        if not approval_ready(current):
            raise ConnectionChanged()


def _check_publication_job(job):
    if isinstance(getattr(job, 'pk', None), int):
        from .models import Job
        if not Job.objects.select_for_update().filter(pk=job.pk, status=Job.LEASED, worker=job.worker,
                attempts=job.attempts, lease_expires__gt=timezone.now()).exists():
            raise ConnectionChanged()


def _renew_job(job, progress=None):
    if job is not None:
        from .jobs import heartbeat
        if not heartbeat(job, progress):
            raise ConnectionChanged()


def _record_collection_failure(source, job, message, period=None, request_count=0):
    """An obsolete worker may record its run failure, but cannot change current status."""
    try:
        with transaction.atomic():
            _check_publication_job(job)
            current = BillingSource.objects.select_for_update().get(pk=source.pk)
            _check_publication_source(current, source)
            if period is not None:
                CollectionPeriod.objects.filter(pk=period.pk).update(
                    status='failed' if period.status != 'complete' else 'partial',
                    attempts=period.attempts + 1, last_attempt=timezone.now(), last_error=message,
                    request_count=request_count)
            BillingSource.objects.filter(pk=source.pk).update(last_error=message)
    except (ConnectionChanged, BillingSource.DoesNotExist):
        return


class _LeasedMeter:
    """Keep paginated work leased, stopping as soon as ownership is lost."""

    def __init__(self, meter, job):
        self.meter, self.job = meter, job

    def call(self, client, operation, **kwargs):
        _renew_job(self.job)
        result = self.meter.call(client, operation, **kwargs)
        # A slow response must not resurrect an expired or replaced lease.
        _renew_job(self.job)
        return result


def safe_error(exc):
    if isinstance(exc, OverlappingBillingScope):
        return (f'Account {exc.account_id} is already collected through another connection ({exc.other}) for the same days. '
                'Retire the duplicate connection or correct the account inventory before retrying.')
    if isinstance(exc, ConnectionChanged):
        return 'The connection changed during collection; results were discarded and the previous snapshot kept.'
    if isinstance(exc, RequestBudgetExceeded):
        return exc.user_message
    if isinstance(exc, ClientError):
        code = exc.response.get('Error', {}).get('Code', 'AWS error')
        explanations = {
            'AccessDenied': 'Check the role trust policy, external ID, and read-only cost permission.',
            'AccessDeniedException': 'Check the role trust policy and read-only cost permission.',
            'DataUnavailableException': 'AWS is preparing billing data. Enable Cost Explorer and try again later.',
            'OptInRequired': 'Enable Cost Explorer in the customer’s AWS console.',
            'LimitExceededException': 'AWS request limit reached; collection will retry with backoff.',
            'ExpiredToken': 'Temporary AWS credentials expired; the job will retry with a fresh session.',
            'ExpiredTokenException': 'Temporary AWS credentials expired; the job will retry with a fresh session.',
            'AWSOrganizationsNotInUseException': 'This account is not an AWS Organizations management account.',
        }
        return f'{code}: {explanations.get(code, "AWS could not complete the request. Check account access and retry.")}'
    if isinstance(exc, ValueError):
        from .redaction import redact
        return f'Validation failed: {redact(str(exc))[:300]}'
    return 'Collection failed. Previous data has been retained; check application logs.'


def month_windows(start, end):
    while start < end:
        stop = min(start.replace(day=1) + relativedelta(months=1), end)
        yield start, stop
        start = stop


def months_back(today, count):
    """Return month starts from oldest to current: ``count`` completed months plus the current month."""
    first = today.replace(day=1) - relativedelta(months=count)
    return [first + relativedelta(months=i) for i in range(count + 1)]


# --- verification & discovery ------------------------------------------------------------

def verify_source(source, session=None, meter=None):
    """Assume the role, confirm the account identity and probe optional capabilities."""
    meter = meter or Meter()
    session = session or Session(source)
    checks = None
    if settings.REQUIRE_CONNECTION_APPROVAL:
        import boto3
        from .aws import AWS_CONFIG
        from .iam import verify_trust
        checks = verify_trust(source, session, boto3.client('sts', region_name=settings.AWS_REGION, config=AWS_CONFIG), meter)
    sts = session.client('sts')
    identity = meter.call(sts, 'get_caller_identity')
    if identity.get('Account') != source.account_id:
        raise ValueError(f'The role belongs to account {identity.get("Account")}, expected {source.account_id}.')
    capabilities = {'cost_explorer': False, 'organizations': False, 'budgets': False}
    if source.collects_costs:
        ce = session.client('ce')
        today = timezone.now().date()
        scope_filter = ({'Filter': {'Dimensions': {'Key': 'LINKED_ACCOUNT', 'Values': [source.account_id]}}}
                        if source.kind == BillingSource.STANDALONE else {})
        meter.call(ce, 'get_cost_and_usage', TimePeriod={'Start': (today - timedelta(days=2)).isoformat(), 'End': today.isoformat()},
                   Granularity='DAILY', Metrics=['UnblendedCost'], **scope_filter)
        capabilities['cost_explorer'] = True
    if source.kind == BillingSource.PAYER and ('organizations' in source.approved_capabilities or not settings.REQUIRE_CONNECTION_APPROVAL):
        try:
            org = meter.call(session.client('organizations'), 'describe_organization')['Organization']
            meter.call(session.client('organizations'),'list_accounts',MaxResults=1)
            capabilities['organizations'] = True
            capabilities['management_account'] = org.get('MasterAccountId', '')
            if capabilities['management_account'] and capabilities['management_account'] != source.account_id:
                capabilities['organizations_note'] = f'This account is a member of organization {capabilities["management_account"]}; linked accounts cannot be listed from here.'
                capabilities['organizations'] = False
        except ClientError as exc:
            capabilities['organizations_error'] = exc.response.get('Error', {}).get('Code', 'AccessDenied')
    if 'budgets' in source.approved_capabilities or not settings.REQUIRE_CONNECTION_APPROVAL:
        try:
            meter.call(session.client('budgets'), 'describe_budgets', AccountId=source.account_id, MaxResults=1)
            capabilities['budgets'] = True
        except ClientError as exc:
            capabilities['budgets_error'] = exc.response.get('Error', {}).get('Code', 'Unavailable')
    for name in ('organizations', 'budgets', 'tags', 'cost_categories', 'forecasts', 'comparison_drivers', 'resources'):
        capabilities.setdefault(name, False)
        if name not in source.approved_capabilities:
            capabilities.setdefault(name + '_error', 'Not approved; core billing remains available.')
    if settings.REQUIRE_CONNECTION_APPROVAL and source.collects_costs:
        window = {'Start': (today - timedelta(days=2)).isoformat(), 'End': today.isoformat()}
        probes = {
            'tags': ('get_tags', {'TimePeriod': window}),
            'cost_categories': ('get_cost_categories', {'TimePeriod': window}),
            'forecasts': ('get_cost_forecast', {'TimePeriod': {'Start': today.isoformat(), 'End': (today + timedelta(days=2)).isoformat()}, 'Metric':'UNBLENDED_COST', 'Granularity':'DAILY'}),
            'resources': ('get_cost_and_usage_with_resources', {'TimePeriod': window, 'Granularity':'DAILY', 'Metrics':['UnblendedCost'], 'Filter': {'Dimensions': {'Key':'SERVICE', 'Values':['Amazon Elastic Compute Cloud - Compute']}}, 'GroupBy':[{'Type':'DIMENSION','Key':'RESOURCE_ID'}]}),
        }
        from dateutil.relativedelta import relativedelta
        month=today.replace(day=1)
        probes['comparison_drivers']=('get_cost_comparison_drivers',{'BaselineTimePeriod':{'Start':str(month-relativedelta(months=2)),'End':str(month-relativedelta(months=1))},'ComparisonTimePeriod':{'Start':str(month-relativedelta(months=1)),'End':str(month)},'MetricForComparison':'UnblendedCost','MaxResults':1})
        for name, (operation, args) in probes.items():
            if name in source.approved_capabilities:
                try:
                    if source.kind == BillingSource.STANDALONE:
                        account_filter = {'Dimensions': {'Key': 'LINKED_ACCOUNT', 'Values': [source.account_id]}}
                        args['Filter'] = ({'And': [args['Filter'], account_filter]} if args.get('Filter') else account_filter)
                    meter.call(ce, operation, **args)
                    capabilities[name] = True
                    if name=='tags':
                        from .models import CustomerApproval
                        approval=CustomerApproval.objects.filter(customer=source.customer).first()
                        permitted=set(approval.metadata if approval else [])
                        active=paginate(meter,ce,'list_cost_allocation_tags','CostAllocationTags',Status='Active')
                        capabilities['active_tag_keys']=[tag['TagKey'] for tag in active if 'tag:'+tag['TagKey'] in permitted]
                except ClientError as exc:
                    capabilities[name + '_error'] = safe_error(exc)
    now = timezone.now()
    with transaction.atomic():
        current = BillingSource.objects.select_for_update().get(pk=source.pk)
        _check_publication_source(current, source)
        updates = dict(capabilities=capabilities, verified_at=now, last_error='', onboarding_step=max(source.onboarding_step, 5))
        if checks is not None:
            updates['trust_checks'] = checks
        BillingSource.objects.filter(pk=source.pk).update(**updates)
    if checks is not None:
        source.trust_checks = checks
    source.capabilities, source.verified_at, source.last_error = capabilities, now, ''
    return capabilities


def discover_accounts(source, session=None, meter=None, today=None):
    """Inventory linked accounts via Organizations, or via Cost Explorer as a billing-only fallback."""
    meter = meter or Meter()
    session = session or Session(source)
    today = today or timezone.now().date()
    now = timezone.now()
    found = {}
    if source.kind == BillingSource.STANDALONE:
        found[source.account_id] = {'name': '', 'state': 'ACTIVE', 'discovery': 'billing'}
        mode = 'standalone'
    elif source.capabilities.get('organizations'):
        for acct in paginate(meter, session.client('organizations'), 'list_accounts', 'Accounts'):
            found[acct['Id']] = {'name': acct.get('Name', ''), 'email': acct.get('Email', ''),
                                 'state': acct.get('State') or acct.get('Status') or 'UNKNOWN',
                                 'joined_at': acct.get('JoinedTimestamp'), 'discovery': 'organizations'}
        mode = 'organizations'
    else:
        start = (today.replace(day=1) - relativedelta(months=settings.HISTORY_MONTHS)).isoformat()
        values = paginate(meter, session.client('ce'), 'get_dimension_values', 'DimensionValues', token_in='NextPageToken',
                          token_out='NextPageToken', TimePeriod={'Start': start, 'End': (today + timedelta(days=1)).isoformat()},
                          Dimension='LINKED_ACCOUNT', Context='COST_AND_USAGE')
        for item in values:
            if item.get('Value'):
                found[item['Value']] = {'name': (item.get('Attributes') or {}).get('description', ''), 'state': 'UNKNOWN', 'discovery': 'billing'}
        found.setdefault(source.account_id, {'name': '', 'state': 'UNKNOWN', 'discovery': 'billing'})
        mode = 'billing_only'
    with transaction.atomic():
        current = BillingSource.objects.select_for_update().get(pk=source.pk)
        _check_publication_source(current, source)
        for account_id, info in found.items():
            account, created = AwsAccount.objects.get_or_create(account_id=account_id, defaults={
                'source': source, 'payer_account_id': source.account_id, 'first_seen': now})
            account.name = info.get('name') or account.name
            from .models import CustomerApproval
            approval = CustomerApproval.objects.filter(customer=source.customer).first() if settings.REQUIRE_CONNECTION_APPROVAL else None
            if not settings.REQUIRE_CONNECTION_APPROVAL or (approval and 'email' in approval.metadata):
                account.email = info.get('email') or account.email
            account.state = info.get('state', account.state)
            account.joined_at = info.get('joined_at') or account.joined_at
            account.discovery = info.get('discovery', account.discovery)
            account.payer_account_id = source.account_id
            account.source = source
            account.last_seen, account.missing_since = now, None
            account.save()
            approved_account = not settings.REQUIRE_CONNECTION_APPROVAL or (approval and account_id in approval.expected_accounts)
            if approved_account and not source.shared and not account.assignments.filter(end__isnull=True).exists():
                ensure_assignment(account, source.customer, note='Auto-assigned from non-shared connection')
        if mode=='organizations':
            AwsAccount.objects.filter(source=source, missing_since__isnull=True).exclude(account_id__in=list(found)).update(missing_since=now)
        BillingSource.objects.filter(pk=source.pk).update(discovered_at=now, discovery_mode=mode, onboarding_step=max(source.onboarding_step, 6), last_error='')
    source.discovered_at, source.discovery_mode = now, mode
    return found


def ensure_assignment(account, customer, start=None, note='', actor=''):
    """Assign an account without overwriting history. A later start ends the prior assignment."""
    with transaction.atomic():
        account = AwsAccount.objects.select_for_update().get(pk=account.pk)
        start = start or EPOCH
        open_assignment = account.assignments.filter(end__isnull=True).order_by('-start').first()
        if open_assignment and open_assignment.customer_id == customer.pk:
            return open_assignment
        if open_assignment:
            if start <= open_assignment.start:
                raise ValueError('The transfer date must be after the current assignment started.')
            open_assignment.end = start
            open_assignment.save(update_fields=['end'])
        assignment = AccountAssignment(account=account, customer=customer, start=start, note=note, created_by=actor,metadata={'name':account.name})
        assignment.full_clean()
        assignment.save()
        return assignment


# --- cost collection ----------------------------------------------------------------------

def fetch_month(ce, meter, first, last, account_id=None, job=None):
    """Fetch daily LINKED_ACCOUNT × SERVICE costs for [first, last) with validation."""
    params = dict(TimePeriod={'Start': first.isoformat(), 'End': last.isoformat()}, Granularity='DAILY',
                  Metrics=['UnblendedCost', 'AmortizedCost'],
                  GroupBy=[{'Type': 'DIMENSION', 'Key': 'LINKED_ACCOUNT'}, {'Type': 'DIMENSION', 'Key': 'SERVICE'}])
    if account_id:
        params['Filter'] = {'Dimensions': {'Key': 'LINKED_ACCOUNT', 'Values': [account_id]}}
    seen_tokens = set()
    records = []
    days = set()
    estimated = False
    page_meter = _LeasedMeter(meter, job)
    while True:
        result = page_meter.call(ce, 'get_cost_and_usage', **params)
        for period in result.get('ResultsByTime', []):
            day = date.fromisoformat(period['TimePeriod']['Start'])
            if not first <= day < last:
                raise ValueError('AWS returned a date outside the requested interval')
            days.add(day)
            estimated = estimated or bool(period.get('Estimated', False))
            for group in period.get('Groups', []):
                if account_id and group['Keys'][0] != account_id:
                    raise ValueError('AWS returned an account outside the approved single-account scope')
                unblended = group['Metrics']['UnblendedCost']
                amortized = group['Metrics']['AmortizedCost']
                if unblended['Unit'] != amortized['Unit']:
                    raise ValueError('Cost metric currencies do not match')
                amount_u, amount_a = Decimal(unblended['Amount']), Decimal(amortized['Amount'])
                if not (amount_u.is_finite() and amount_a.is_finite()):
                    raise ValueError('AWS returned a non-finite amount')
                records.append(dict(day=day, account_id=group['Keys'][0], service=group['Keys'][1], currency=unblended['Unit'],
                                    unblended=amount_u, amortized=amount_a, estimated=bool(period.get('Estimated', False))))
        token = result.get('NextPageToken')
        if not token:
            break
        if token in seen_tokens:
            raise ValueError('Repeated AWS pagination token')
        seen_tokens.add(token)
        params['NextPageToken'] = token
    return records, days, estimated


def owner_lookup(source, records):
    """Map (account_id, day) to the owning customer; inventory unknown accounts."""
    owners = {}
    account_ids = {r['account_id'] for r in records}
    accounts = {a.account_id: a for a in AwsAccount.objects.filter(account_id__in=account_ids).prefetch_related('assignments')}
    now = timezone.now()
    for account_id in account_ids:
        account = accounts.get(account_id)
        if account is None:
            account = AwsAccount.objects.create(account_id=account_id, source=source, payer_account_id=source.account_id,
                                                state='UNKNOWN', discovery='billing', first_seen=now, last_seen=now)
            accounts[account_id] = account
        from .models import CustomerApproval
        approval=CustomerApproval.objects.filter(customer=source.customer,status='approved').first() if settings.REQUIRE_CONNECTION_APPROVAL else None
        approved=not settings.REQUIRE_CONNECTION_APPROVAL or (approval and account_id in approval.expected_accounts)
        if approved and not source.shared and not account.assignments.filter(end__isnull=True).exists():
            ensure_assignment(account, source.customer, note='Auto-assigned from billing data')
            # Inventory accounts were prefetched before this assignment existed.
            # Read the updated history before stamping the first published costs.
            assignments = list(AccountAssignment.objects.filter(account=account))
        else:
            assignments = list(account.assignments.all())
        for record in (r for r in records if r['account_id'] == account_id):
            match = next((a for a in assignments if a.covers(record['day'])), None)
            owners[(account_id, record['day'])] = match.customer_id if match else None
    return owners


def check_overlap(source, records, first, last):
    """Reject rows that another connection already collected for the same account and day."""
    account_ids = {r['account_id'] for r in records}
    if not account_ids:
        return
    other = Cost.objects.exclude(source=source).filter(account_id__in=account_ids, day__gte=first, day__lt=last).values_list('account_id', 'day', 'source__account_id').distinct()
    pairs = {(r['account_id'], r['day']) for r in records}
    for account_id, day, other_account in other:
        if (account_id, day) in pairs:
            raise OverlappingBillingScope(account_id, f'connection {other_account}')


def publish_month(source, month, records, days, estimated, meter, started, attempts=1, job=None):
    """Atomically replace one month for one source. Old rows survive any failure."""
    first, last = month, month + relativedelta(months=1)
    period, _ = CollectionPeriod.objects.get_or_create(source=source, month=month)
    with transaction.atomic():
        _check_publication_job(job)
        locked = BillingSource.objects.select_for_update().get(pk=source.pk)
        _check_publication_source(locked, source)
        if locked.kind == BillingSource.STANDALONE and any(r['account_id'] != locked.account_id for r in records):
            raise ValueError('Cannot publish costs outside the approved single-account scope')
        from django.db import connection
        if connection.vendor=='postgresql':
            import hashlib
            # Different source leases still must not publish the same account/day.
            # Stable ordered account locks cover first-ever imports as well.
            with connection.cursor() as cursor:
                for account in sorted({r['account_id'] for r in records}):
                    key=int.from_bytes(hashlib.sha256(account.encode()).digest()[:8],'big',signed=True)
                    cursor.execute('SELECT pg_advisory_xact_lock(%s)',[key])
        check_overlap(source, records, first, last)
        owners = owner_lookup(source, records)
        rows = [Cost(source=source, customer_id=owners[(r['account_id'], r['day'])], **r) for r in records]
        Cost.objects.filter(source=source, day__gte=first, day__lt=last).delete()
        Cost.objects.bulk_create(rows, batch_size=1000)
        now = timezone.now()
        CollectionPeriod.objects.filter(pk=period.pk).update(
            status='complete', revision=period.revision + 1, attempts=period.attempts + attempts, rows=len(rows),
            request_count=meter.requests, page_count=meter.pages, duration_ms=int((now - started).total_seconds() * 1000),
            first_day=min(days) if days else None, last_day=max(days) if days else None, estimated=estimated,
            last_attempt=now, last_success=now, last_error='')
    return len(rows)


def collect_source(source, months=None, session=None, meter=None, today=None, job=None, client=None):
    """Collect the given months for one source. Returns the SyncRun."""
    if not source.enabled or not source.role_arn or not source.collects_costs:
        return None
    today = today or timezone.now().date()
    meter = meter or Meter()
    if months is None:
        months = months_back(today, settings.HISTORY_MONTHS if not source.initial_import_done else 1)
    months = sorted(m for m in months if m <= today)
    progress = dict(job.progress) if job is not None else {}
    completed = set(progress.get('completed', []))
    run = SyncRun.objects.create(source=source, customer=source.customer)
    BillingSource.objects.filter(pk=source.pk).update(last_attempt=run.started_at, sync_requested=False)
    total_rows = 0
    try:
        ce = client or (session or Session(source)).client('ce')
        for month in months:
            if month.isoformat() in completed:
                continue
            first, last = month, min(month + relativedelta(months=1), today + timedelta(days=1))
            period, _ = CollectionPeriod.objects.get_or_create(source=source, month=month)
            started = timezone.now()
            before = meter.requests
            try:
                records, days, estimated = fetch_month(
                    ce, meter, first, last,
                    account_id=source.account_id if source.kind == BillingSource.STANDALONE else None, job=job)
                total_rows += publish_month(source, month, records, days, estimated, meter, started,job=job)
            except Exception as exc:
                _record_collection_failure(source, job, safe_error(exc), period, meter.requests - before)
                raise
            completed.add(month.isoformat())
            progress['completed'] = sorted(completed)
            _renew_job(job, progress)
        now = timezone.now()
        with transaction.atomic():
            _check_publication_job(job)
            current = BillingSource.objects.select_for_update().get(pk=source.pk)
            _check_publication_source(current, source)
            BillingSource.objects.filter(pk=source.pk).update(last_success=now, last_error='', initial_import_done=True)
        run.status, run.rows, run.requests, run.finished_at = 'success', total_rows, meter.requests, now
        run.save()
        return run
    except Exception as exc:
        run.status, run.error, run.rows, run.requests, run.finished_at = 'failed', safe_error(exc), total_rows, meter.requests, timezone.now()
        run.save()
        _record_collection_failure(source, job, run.error)
        logger.warning('Collection failed source=%s type=%s', source.pk, type(exc).__name__)
        if job is not None:
            job.progress = progress
        raise


# --- AWS budgets import --------------------------------------------------------------------

def to_decimal(value):
    try:
        return Decimal(str(value)) if value not in (None, '') else None
    except Exception:
        return None


def import_budgets(source, session=None, meter=None, job=None):
    """Read-only snapshot of AWS Budgets owned by the connected account (paginated)."""
    meter = meter or Meter()
    session = session or Session(source)
    client = session.client('budgets')
    items = paginate(_LeasedMeter(meter, job), client, 'describe_budgets', 'Budgets', AccountId=source.account_id, MaxResults=100)
    now = timezone.now()
    snapshots = []
    for b in items:
        limit = b.get('BudgetLimit') or {}
        calculated = b.get('CalculatedSpend') or {}
        actual = calculated.get('ActualSpend') or {}
        forecast = calculated.get('ForecastedSpend') or {}
        period = b.get('TimePeriod') or {}
        snapshots.append(ImportedBudget(
            source=source, owning_account_id=source.account_id, name=b['BudgetName'], arn=b.get('BudgetArn', '') if isinstance(b.get('BudgetArn'), str) else '',
            budget_type=b.get('BudgetType', ''), time_unit=b.get('TimeUnit', ''),
            limit_amount=to_decimal(limit.get('Amount')), limit_unit=limit.get('Unit', ''),
            time_period={k: v.isoformat() if hasattr(v, 'isoformat') else v for k, v in period.items()},
            filters=b.get('CostFilters') or b.get('FilterExpression') or {},
            actual_amount=to_decimal(actual.get('Amount')), actual_unit=actual.get('Unit', ''),
            forecast_amount=to_decimal(forecast.get('Amount')), forecast_unit=forecast.get('Unit', ''),
            calculated_at=b.get('LastUpdatedTime'), aws_updated_at=b.get('LastUpdatedTime'),
            raw=json.loads(json.dumps({k: v for k, v in b.items() if k not in ('CalculatedSpend', 'BudgetLimit', 'TimePeriod', 'CostFilters', 'FilterExpression')}, cls=DjangoJSONEncoder)),
            imported_at=now))
    with transaction.atomic():
        _check_publication_job(job)
        current = BillingSource.objects.select_for_update().get(pk=source.pk)
        _check_publication_source(current, source)
        ImportedBudget.objects.filter(source=source).delete()
        ImportedBudget.objects.bulk_create(snapshots)
    return len(snapshots)
