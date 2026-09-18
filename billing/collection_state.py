"""One dashboard presentation of connection readiness and collection progress."""
from django.conf import settings
from django.utils import timezone
from . import scheduler
from .models import Job


def snapshot(source, recent_jobs=(), *, can_edit=False):
    initialized = scheduler.collection_initialized(source)
    reason = scheduler.collection_readiness(source)
    kinds = {'collect'} if source.collects_costs else set()
    if 'budgets' in source.approved_capabilities or (not settings.REQUIRE_CONNECTION_APPROVAL and source.capabilities.get('budgets')):
        kinds.add('import_budgets')
    data_jobs = [job for job in recent_jobs if job.kind in kinds
                 and job.payload.get('connection_version') in (None, source.connection_version)]
    pending = any(job.status in (Job.QUEUED, Job.LEASED) for job in data_jobs)
    latest_by_kind = {}
    for job in sorted(data_jobs, key=lambda item: item.created_at, reverse=True):
        latest_by_kind.setdefault(job.kind, job)
    failed = next((job for job in latest_by_kind.values() if job.status == Job.FAILED), None)
    error = (failed.last_error if failed else '') or source.last_error
    if reason:
        status = 'Paused' if not source.enabled or not source.customer.active else 'Needs connection'
    elif pending:
        status = 'Pulling data' if any(job.status == Job.LEASED for job in data_jobs) else 'Pull queued'
    elif error:
        status = 'Needs attention'
    elif not initialized:
        status = 'Preparing automatic pull'
    else:
        status = 'Automatic refresh enabled'
    now = timezone.now()
    next_run = None
    if not reason:
        if not initialized and not pending and not error:
            next_run = now
        else:
            next_run = source.next_run if source.next_run and source.next_run > now else scheduler.next_slot(now)
    return {
        'source': source, 'status': status, 'last_success': source.last_success,
        'can_manage_source': can_edit,
        'error': error or reason, 'pending': pending, 'collection_pending': pending,
        'collection_initialized': initialized, 'collection_started': initialized or bool(data_jobs),
        'ready_to_pull': not initialized and not reason and not pending and not error,
        'can_pull': can_edit and not reason, 'can_refresh': can_edit and initialized and not reason,
        'next_collection_at': next_run,
    }
