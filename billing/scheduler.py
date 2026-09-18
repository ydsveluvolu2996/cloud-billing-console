"""Six-hour scheduling and job handlers.

Ready new connections pull their initial history immediately. Thereafter, slots at 00:00,
06:00, 12:00 and 18:00 UTC use configurable jitter and idempotent keys. Older months are
reconciled on the weekly and monthly cadence. Manual requests share pending work.
"""
import logging
import random
from datetime import timedelta, timezone as datetime_timezone
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Q
from django.utils.dateparse import parse_datetime
from django.utils import timezone
from . import collector, jobs
from .aws import Meter, Session
from .models import BillingSource, Job

SLOT_HOURS = 6
logger = logging.getLogger(__name__)


def slot_for(now):
    hour = (now.hour // SLOT_HOURS) * SLOT_HOURS
    return now.replace(hour=hour, minute=0, second=0, microsecond=0)


def next_slot(now):
    return slot_for(now) + timedelta(hours=SLOT_HOURS)


def reconcile_months(slot):
    """How many completed months to refresh at this slot (current month is always included)."""
    if slot.day == 2 and slot.hour == 0:
        return settings.HISTORY_MONTHS  # monthly full-history reconciliation for late revisions and credits
    if slot.weekday() == 6 and slot.hour == 0:
        return getattr(settings, 'RECONCILE_MONTHS', 3)
    return 1  # current and previous month


def active_sources():
    return BillingSource.objects.filter(enabled=True, customer__active=True, kind__in=[BillingSource.PAYER, BillingSource.STANDALONE]).exclude(role_arn='').exclude(verified_at=None)


def collection_initialized(source):
    if source.collects_costs:
        return source.initial_import_done
    return bool(source.initial_import_done or source.last_success or source.capabilities.get('budgets_imported_at'))


def collection_readiness(source):
    """An empty reason means collection is authorized and setup has finished."""
    if not source.enabled or not source.customer.active:
        return 'Resume this connection and customer before pulling data.'
    if not source.role_arn or not source.verified_at:
        return 'Connect and verify this AWS account before pulling data.'
    if source.activation_requests.filter(status__in=['queued', 'processing', 'verifying', 'importing']).exists():
        return 'Wait for the connection setup to finish before pulling data.'
    if source.collects_costs and not source.discovered_at:
        return 'Wait for account discovery to finish before pulling data.'
    if settings.REQUIRE_CONNECTION_APPROVAL:
        from .iam import approval_ready
        checks = {'correct_external_id': 'passed', 'missing_external_id': 'denied', 'wrong_external_id': 'denied',
                  'account_identity': 'passed', 'exact_collector_principal': 'passed', 'connection_version': source.connection_version}
        if not approval_ready(source):
            return 'Current customer and role approval are required before pulling data.'
        if any(source.trust_checks.get(key) != value for key, value in checks.items()):
            return 'Verify the current connection settings before pulling data.'
    if not source.collects_costs and not _imports_budgets(source):
        return 'Approve AWS budget access before pulling data.'
    return ''


def _imports_budgets(source):
    return 'budgets' in source.approved_capabilities or (not settings.REQUIRE_CONNECTION_APPROVAL and source.capabilities.get('budgets'))


def _pending_job(source, kind):
    return Job.objects.filter(source=source, kind=kind, status__in=[Job.QUEUED, Job.LEASED]).filter(
        Q(payload__connection_version=source.connection_version) | Q(payload__connection_version__isnull=True)).order_by('created_at').first()


def _budget_success(source):
    value = source.capabilities.get('budgets_imported_at')
    try:
        imported = parse_datetime(value) if isinstance(value, str) else None
    except ValueError:
        imported = None
    if imported and timezone.is_naive(imported):
        imported = imported.replace(tzinfo=datetime_timezone.utc)
    if not source.collects_costs:
        return max((value for value in (source.last_success, imported) if value), default=None)
    return imported


def schedule_due(now=None):
    """Start ready accounts immediately, then catch up one six-hour slot at a time."""
    now = now or timezone.now()
    slot = slot_for(now)
    stamp = slot.strftime('%Y%m%dT%H')
    created = 0
    source_ids = BillingSource.objects.filter(enabled=True, customer__active=True).exclude(role_arn='').exclude(verified_at=None).values_list('pk', flat=True)
    for source_id in list(source_ids):
        try:
            with transaction.atomic():
                source = BillingSource.objects.select_for_update().get(pk=source_id)
                if collection_readiness(source):
                    BillingSource.objects.filter(pk=source.pk).update(next_run=None)
                    continue
                _, made_for_source = _schedule_source(source, now)
                created += made_for_source
        except BillingSource.DoesNotExist:
            continue
        except Exception:
            # A malformed or concurrently edited account cannot stop scheduling
            # other customers; its transaction is rolled back and the next tick retries.
            logger.exception('Could not schedule billing source %s', source_id)
    _, made = jobs.enqueue('evaluate_budgets', key=f'evaluate_budgets:{stamp}', priority=9, once=True, run_after=slot + timedelta(minutes=45))
    created += made
    _, made = jobs.enqueue('allocate_projects', key=f'allocate_projects:{stamp}', priority=9, once=True, run_after=slot + timedelta(minutes=40))
    created += made
    _, made = jobs.enqueue('monitor_operations',key=f'monitor:{stamp}',priority=9,once=True,run_after=slot+timedelta(minutes=50))
    created += made
    return created


def _slot_key(source, kind, now):
    return f'{kind}:{source.pk}:v{source.connection_version}:{slot_for(now):%Y%m%dT%H}'


def _schedule_source(source, now):
    """The caller holds the source lock and has checked collection readiness."""
    initial = not collection_initialized(source)
    slot = slot_for(now)
    stamp = slot.strftime('%Y%m%dT%H')
    jitter_seconds = getattr(settings, 'SCHEDULE_JITTER_SECONDS', 900)
    run_after = now if initial else slot + timedelta(seconds=random.uniform(0, jitter_seconds))
    created = 0
    primary = None
    if source.collects_costs and (initial or not source.last_success or source.last_success < slot):
        primary = _pending_job(source, 'collect')
        if primary is None:
            primary, made = jobs.enqueue('collect', key=_slot_key(source, 'collect', now), source=source,
                priority=3 if initial else 5, once=True, run_after=run_after,
                payload={'months_back': settings.HISTORY_MONTHS if initial else reconcile_months(slot),
                         'initial': initial, 'slot': stamp})
            created += made
        if not initial:
            _, made = jobs.enqueue('explorer_refresh', key=_slot_key(source, 'explorer_refresh', now), source=source,
                priority=7, once=True, run_after=run_after + timedelta(minutes=5), payload={'scheduled': True})
            created += made
            if slot.hour == 0:
                _, made = jobs.enqueue('discover', key=_slot_key(source, 'discover', now), source=source,
                    priority=6, once=True, run_after=run_after)
                created += made
    budget_success = _budget_success(source)
    if _imports_budgets(source) and (not budget_success or budget_success < slot):
        budget_job = _pending_job(source, 'import_budgets')
        if budget_job is None:
            budget_job, made = jobs.enqueue('import_budgets', key=_slot_key(source, 'import_budgets', now), source=source,
                priority=4 if initial else 8, once=True,
                run_after=run_after if initial else run_after + timedelta(minutes=2),
                payload={'initial': initial, 'slot': stamp})
            created += made
        primary = primary or budget_job
    future = primary.run_after if primary and primary.status == Job.QUEUED and primary.run_after > now else next_slot(now)
    BillingSource.objects.filter(pk=source.pk).update(next_run=future, sync_requested=False)
    return primary, created


def request_refresh(source, actor='', months_back=1, actor_id=None):
    """Coalesced manual refresh: repeated clicks share one queued job."""
    return _request_pull(source, actor=actor, actor_id=actor_id, months_back=months_back)


def request_verification(source, actor=''):
    return jobs.enqueue('verify', key=f'verify:{source.pk}:v{source.connection_version}', source=source, priority=1, max_attempts=1, payload={'actor': actor})


def request_discovery(source, actor=''):
    return jobs.enqueue('discover', key=f'discover:{source.pk}:manual:v{source.connection_version}', source=source, priority=2, max_attempts=2, payload={'actor': actor})


def request_initial_import(source, actor='', actor_id=None):
    return _request_pull(source, actor=actor, actor_id=actor_id, months_back=settings.HISTORY_MONTHS, initial=True)


def _request_pull(source, *, actor, actor_id, months_back, initial=False):
    with transaction.atomic():
        source = BillingSource.objects.select_for_update().get(pk=source.pk)
        reason = collection_readiness(source)
        if reason:
            raise ValidationError(reason)
        initial = initial or not collection_initialized(source)
        payload = {'months_back': settings.HISTORY_MONTHS if initial else months_back, 'manual': True, 'initial': initial, 'actor': actor}
        if actor_id is not None:
            payload['actor_id'] = actor_id
        primary = None
        for kind in (['collect'] if source.collects_costs else []) + (['import_budgets'] if _imports_budgets(source) else []):
            pending = _pending_job(source, kind)
            suffix = 'manual'
            if pending and kind == 'collect' and payload['months_back'] > pending.payload.get('months_back', 1):
                if pending.status == Job.QUEUED:
                    pending.payload = dict(pending.payload, **payload, connection_version=source.connection_version)
                    pending.save(update_fields=['payload'])
                else:
                    pending = None  # A running shorter refresh cannot satisfy a requested backfill.
                    suffix = 'backfill'
            # Initial automatic and manual requests share a slot key. An explicit
            # retry can create new work; scheduler ticks cannot reset its retry budget.
            key = _slot_key(source, kind, timezone.now()) if initial and suffix != 'backfill' else f'{kind}:{source.pk}:{suffix}:v{source.connection_version}'
            result = (pending, False) if pending else jobs.enqueue(kind, key=key,
                source=source, priority=3, payload=payload)
            if primary is None:
                primary = result
        BillingSource.objects.filter(pk=source.pk).update(sync_requested=False)
        return primary


def queue_summary():
    counts = {status: 0 for status in (Job.QUEUED, Job.LEASED, Job.FAILED, Job.DONE)}
    for row in Job.objects.values('status').annotate(n=Count('pk')):
        counts[row['status']] = row['n']
    return counts


# --- handlers ---------------------------------------------------------------------------------

def load_source(job):
    source = BillingSource.objects.select_related('customer').filter(pk=job.source_id).first()
    if source is None:
        raise jobs.PermanentJobError('The connection no longer exists.')
    version = job.payload.get('connection_version')
    if version is not None and version != source.connection_version:
        raise jobs.PermanentJobError('The connection changed after this request. Pull data again for the current settings.')
    if not source.enabled or not source.customer.active:
        raise jobs.PermanentJobError('The connection is paused or the customer is offboarded.')
    if not source.role_arn:
        raise jobs.PermanentJobError('The connection has no role ARN yet.')
    if settings.REQUIRE_CONNECTION_APPROVAL:
        from .iam import assert_role_allowed
        try:
            assert_role_allowed(source)
        except ValueError as exc:
            raise jobs.PermanentJobError(str(exc)) from None
        if job.kind != 'verify' and (not source.verified_at or source.trust_checks.get('connection_version') != source.connection_version):
            raise jobs.PermanentJobError('Current connection trust validation is required.')
    actor_id = job.payload.get('actor_id')
    if actor_id and job.kind != 'explorer_refresh':
        from django.contrib.auth.models import User
        from .access import for_user
        actor = User.objects.only('id','username','is_active','is_superuser','is_staff').filter(pk=actor_id,is_active=True).first()
        access = for_user(actor) if actor else None
        permitted = access.customers if access and job.kind=='explorer_refresh' else access.editable if access else ()
        if access is None or (not access.portfolio and source.customer_id not in permitted):
            raise jobs.PermanentJobError('The requesting operator no longer has customer access.')
    return source


@jobs.handler('verify')
def handle_verify(job):
    source = load_source(job)
    try:
        capabilities = collector.verify_source(source)
        if (job.payload.get('activation_id') and 'budgets' in source.approved_capabilities
                and not capabilities.get('budgets')
                and capabilities.get('budgets_error') in ('AccessDenied', 'AccessDeniedException', 'Throttling', 'ThrottlingException', 'TooManyRequestsException')):
            from botocore.exceptions import ClientError
            BillingSource.objects.filter(pk=source.pk, connection_version=source.connection_version).update(verified_at=None)
            raise ClientError({'Error': {'Code': capabilities['budgets_error']}}, 'DescribeBudgets')
    except Exception as exc:
        message = collector.safe_error(exc)
        BillingSource.objects.filter(pk=source.pk, connection_version=source.connection_version).update(last_error=message)
        from botocore.exceptions import ClientError
        if job.payload.get('activation_id') and isinstance(exc, ClientError):
            code = exc.response.get('Error', {}).get('Code', '')
            if code in ('AccessDenied', 'AccessDeniedException', 'Throttling', 'ThrottlingException', 'TooManyRequestsException', 'ServiceUnavailable'):
                # Newly installed IAM policies may not yet be visible to STS.
                # The activation job has a bounded attempt count and backoff;
                # trust-validation ValueErrors are still permanent failures.
                raise
        raise jobs.PermanentJobError(message)
    if source.collects_costs:
        request_discovery(source)
    if capabilities.get('budgets') and collection_initialized(source):
        jobs.enqueue('import_budgets', key=f'import_budgets:{source.pk}:verified:v{source.connection_version}', source=source, priority=2)
    return {'capabilities': capabilities}


@jobs.handler('discover')
def handle_discover(job):
    source = load_source(job)
    if not source.collects_costs:
        return {'accounts': 0, 'mode': 'member_budgets'}
    try:
        found = collector.discover_accounts(source)
    except Exception as exc:
        BillingSource.objects.filter(pk=source.pk, connection_version=source.connection_version).update(last_error=collector.safe_error(exc))
        raise
    return {'accounts': len(found), 'mode': source.discovery_mode}


@jobs.handler('collect')
def handle_collect(job):
    source = load_source(job)
    today = timezone.now().date()
    months_back = job.payload.get('months_back', 1)
    if not source.initial_import_done:
        months_back = settings.HISTORY_MONTHS
    months = collector.months_back(today, months_back)
    session = Session(source)
    run = collector.collect_source(source, months=months, session=session, meter=Meter(), today=today, job=job)
    progress = dict(job.progress or {})
    progress.update(rows=run.rows if run else 0, requests=run.requests if run else 0)
    return progress


@jobs.handler('explorer_refresh')
def handle_explorer_refresh(job):
    from .query_cache import refresh_queries
    source = load_source(job)
    return {'processed': refresh_queries(source=source, scheduled=job.payload.get('scheduled', False))}


@jobs.handler('import_budgets')
def handle_import_budgets(job):
    source = load_source(job)
    if settings.REQUIRE_CONNECTION_APPROVAL and 'budgets' not in source.approved_capabilities:
        raise jobs.PermanentJobError('Budget import approval is required for this connection.')
    from botocore.exceptions import ClientError
    try:
        count = collector.import_budgets(source, job=job)
    except ClientError as exc:
        code = exc.response.get('Error', {}).get('Code', '')
        if code not in ('AccessDenied', 'AccessDeniedException', 'UnauthorizedOperation'):
            raise
        caps = dict(source.capabilities, budgets=False, budgets_error=code)
        BillingSource.objects.filter(pk=source.pk, connection_version=source.connection_version).update(capabilities=caps)
        raise jobs.PermanentJobError('AWS budget read denied. Grant budgets:ViewBudget to this account connection.') from None
    caps = dict(source.capabilities, budgets=True, budgets_imported_at=timezone.now().isoformat())
    caps.pop('budgets_error', None)
    updates = {'capabilities': caps}
    if not source.collects_costs:
        updates.update(last_error='', last_success=timezone.now(), initial_import_done=True, sync_requested=False)
    BillingSource.objects.filter(pk=source.pk, connection_version=source.connection_version).update(**updates)
    return {'budgets': count}


@jobs.handler('evaluate_budgets')
def handle_evaluate_budgets(job):
    from .budgets import evaluate_all
    return {'evaluated': evaluate_all()}


@jobs.handler('allocate_projects')
def handle_allocate_projects(job):
    from .allocation import allocate_all
    return {'projects': allocate_all()}


@jobs.handler('monitor_operations')
def handle_monitor(job):
    from .monitoring import scan,deliver
    return {'new_alerts':scan(),'delivered':deliver()}
