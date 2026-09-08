"""Six-hour scheduling and job handlers.

Slots: 00:00, 06:00, 12:00 and 18:00 UTC. Each active source gets one collect job per slot
with configurable jitter; keys make scheduling idempotent. Older months are reconciled on the
weekly and monthly cadence. Manual refresh requests are coalesced into one job per source.
"""
import random
from datetime import timedelta
from django.conf import settings
from django.db.models import Count
from django.utils import timezone
from . import collector, jobs
from .aws import Meter, Session
from .models import BillingSource, Job

SLOT_HOURS = 6


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


def schedule_due(now=None):
    """Idempotently enqueue the current slot's work plus manual refresh requests."""
    now = now or timezone.now()
    slot = slot_for(now)
    stamp = slot.strftime('%Y%m%dT%H')
    jitter_seconds = getattr(settings, 'SCHEDULE_JITTER_SECONDS', 900)
    created = 0
    for source in active_sources():
        run_after = slot + timedelta(seconds=random.uniform(0, jitter_seconds))
        _, made = jobs.enqueue('collect', key=f'collect:{source.pk}:{stamp}', source=source, priority=5, once=True,
                               run_after=run_after, payload={'months_back': reconcile_months(slot), 'slot': stamp})
        created += made
        _, made = jobs.enqueue('explorer_refresh', key=f'explorer_refresh:{source.pk}:{stamp}', source=source, priority=7, once=True,
                               run_after=run_after + timedelta(minutes=5), payload={'scheduled': True})
        created += made
        if source.capabilities.get('budgets'):
            _, made = jobs.enqueue('import_budgets', key=f'import_budgets:{source.pk}:{stamp}', source=source, priority=8, once=True,
                                   run_after=run_after + timedelta(minutes=2))
            created += made
        if slot.hour == 0:
            _, made = jobs.enqueue('discover', key=f'discover:{source.pk}:{stamp}', source=source, priority=6, once=True, run_after=run_after)
            created += made
        BillingSource.objects.filter(pk=source.pk).update(next_run=run_after if source.next_run is None or source.next_run < now else source.next_run)
    for source in BillingSource.objects.filter(kind=BillingSource.MEMBER_BUDGETS, enabled=True, customer__active=True).exclude(role_arn='').exclude(verified_at=None):
        _, made = jobs.enqueue('import_budgets', key=f'import_budgets:{source.pk}:{stamp}', source=source, priority=8, once=True, run_after=slot)
        created += made
    for source in active_sources().filter(sync_requested=True):
        _, made = jobs.enqueue('collect', key=f'collect:{source.pk}:manual', source=source, priority=3, payload={'months_back': 1, 'manual': True})
        created += made
    _, made = jobs.enqueue('evaluate_budgets', key=f'evaluate_budgets:{stamp}', priority=9, once=True, run_after=slot + timedelta(minutes=45))
    created += made
    _, made = jobs.enqueue('allocate_projects', key=f'allocate_projects:{stamp}', priority=9, once=True, run_after=slot + timedelta(minutes=40))
    created += made
    return created


def request_refresh(source, actor='', months_back=1):
    """Coalesced manual refresh: repeated clicks share one queued job."""
    BillingSource.objects.filter(pk=source.pk).update(sync_requested=True)
    return jobs.enqueue('collect', key=f'collect:{source.pk}:manual', source=source, priority=3, payload={'months_back': months_back, 'manual': True, 'actor': actor})


def request_verification(source, actor=''):
    return jobs.enqueue('verify', key=f'verify:{source.pk}', source=source, priority=1, max_attempts=1, payload={'actor': actor})


def request_discovery(source, actor=''):
    return jobs.enqueue('discover', key=f'discover:{source.pk}:manual', source=source, priority=2, max_attempts=2, payload={'actor': actor})


def request_initial_import(source, actor=''):
    BillingSource.objects.filter(pk=source.pk).update(sync_requested=True)
    return jobs.enqueue('collect', key=f'collect:{source.pk}:initial', source=source, priority=4,
                        payload={'months_back': settings.HISTORY_MONTHS, 'initial': True, 'actor': actor})


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
    if not source.enabled or not source.customer.active:
        raise jobs.PermanentJobError('The connection is paused or the customer is offboarded.')
    if not source.role_arn:
        raise jobs.PermanentJobError('The connection has no role ARN yet.')
    return source


@jobs.handler('verify')
def handle_verify(job):
    source = load_source(job)
    try:
        capabilities = collector.verify_source(source)
    except Exception as exc:
        message = collector.safe_error(exc)
        BillingSource.objects.filter(pk=source.pk).update(last_error=message)
        raise jobs.PermanentJobError(message)
    request_discovery(source)
    return {'capabilities': capabilities}


@jobs.handler('discover')
def handle_discover(job):
    source = load_source(job)
    try:
        found = collector.discover_accounts(source)
    except Exception as exc:
        BillingSource.objects.filter(pk=source.pk).update(last_error=collector.safe_error(exc))
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
    if not source.capabilities.get('budgets'):
        raise jobs.PermanentJobError('budgets:ViewBudget is not granted for this connection.')
    return {'budgets': collector.import_budgets(source)}


@jobs.handler('evaluate_budgets')
def handle_evaluate_budgets(job):
    from .budgets import evaluate_all
    return {'evaluated': evaluate_all()}


@jobs.handler('allocate_projects')
def handle_allocate_projects(job):
    from .allocation import allocate_all
    return {'projects': allocate_all()}
