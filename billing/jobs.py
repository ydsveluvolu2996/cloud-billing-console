"""Durable PostgreSQL-backed job queue with leases, coalescing and fair per-source scheduling.

Web requests only enqueue. A separately supervised worker (``manage.py run_worker``) leases
jobs with ``SELECT ... FOR UPDATE SKIP LOCKED``, extends leases while it makes progress and
re-queues work with exponential backoff and jitter when it fails or when a worker crashes.
"""
import logging
import random
import socket
import threading
import time
import uuid
from datetime import timedelta
from django.conf import settings
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.utils import timezone
from .models import BillingSource, Job

logger = logging.getLogger(__name__)
HANDLERS = {}


class PermanentJobError(Exception):
    """Do not retry: configuration or permission problems that need an operator."""


def handler(kind):
    def register(fn):
        HANDLERS[kind] = fn
        return fn
    return register


def setting(name, default):
    return getattr(settings, name, default)


def jitter(seconds):
    return timedelta(seconds=random.uniform(0, max(seconds, 0)))


def backoff(attempts):
    base = setting('JOB_BACKOFF_SECONDS', 60) * 2 ** max(attempts - 1, 0)
    delay = min(base, setting('JOB_BACKOFF_CAP_SECONDS', 3600))
    return timedelta(seconds=delay) + jitter(delay / 2)


def enqueue(kind, key=None, source=None, payload=None, priority=5, run_after=None, max_attempts=None, once=False):
    """Create or coalesce a job. Returns (job, created)."""
    key = key or f'{kind}:{source.pk if source else "global"}'
    run_after = run_after or timezone.now()
    with transaction.atomic():
        if once and Job.objects.filter(key=key).exists():
            return Job.objects.filter(key=key).order_by('-created_at').first(), False
        existing = Job.objects.select_for_update().filter(key=key, status__in=[Job.QUEUED, Job.LEASED]).first()
        if existing:
            if existing.status == Job.QUEUED and run_after < existing.run_after:
                Job.objects.filter(pk=existing.pk).update(run_after=run_after, priority=min(priority, existing.priority))
            return existing, False
        try:
            with transaction.atomic():
                job = Job.objects.create(kind=kind, key=key, source=source, payload=payload or {}, priority=priority,
                                         run_after=run_after, max_attempts=max_attempts or setting('JOB_MAX_ATTEMPTS', 6))
                return job, True
        except IntegrityError:
            return Job.objects.filter(key=key, status__in=[Job.QUEUED, Job.LEASED]).first(), False


def lease(worker, now=None, kinds=None):
    """Lease the next runnable job. Sources with a leased job are skipped (per-source exclusion)."""
    now = now or timezone.now()
    with transaction.atomic():
        busy = Job.objects.filter(status=Job.LEASED, source__isnull=False).values_list('source_id', flat=True)
        candidates = Job.objects.filter(status=Job.QUEUED, run_after__lte=now).exclude(source_id__in=list(busy))
        if kinds:
            candidates = candidates.filter(kind__in=kinds)
        candidates = candidates.order_by('priority', 'run_after', 'pk')
        if connection.vendor == 'postgresql':
            candidates = candidates.select_for_update(skip_locked=True)
        else:
            candidates = candidates.select_for_update()
        job = None
        for candidate in candidates.iterator(chunk_size=1):
            if candidate.source_id:
                sources = BillingSource.objects.filter(pk=candidate.source_id)
                sources = sources.select_for_update(skip_locked=True) if connection.vendor == 'postgresql' else sources.select_for_update()
                # Different job rows can share a source. Serialize the claim on the
                # source row, then recheck after any other claimant committed.
                if sources.first() is None:
                    continue
                if Job.objects.filter(source_id=candidate.source_id, status=Job.LEASED).exists():
                    continue
            job = candidate
            break
        if not job:
            return None
        job.status = Job.LEASED
        job.worker = worker
        job.attempts += 1
        job.started_at = now
        job.lease_expires = now + timedelta(seconds=setting('JOB_LEASE_SECONDS', 600))
        job.save(update_fields=['status', 'worker', 'attempts', 'started_at', 'lease_expires'])
        return job


def heartbeat(job, progress=None):
    if not isinstance(getattr(job, 'pk', None), int):
        if progress is not None:
            job.progress = progress
        return
    fields = {'lease_expires': timezone.now() + timedelta(seconds=setting('JOB_LEASE_SECONDS', 600))}
    if progress is not None:
        job.progress = progress
        fields['progress'] = progress
    Job.objects.filter(pk=job.pk, status=Job.LEASED).update(**fields)


def complete(job, progress=None):
    now = timezone.now()
    Job.objects.filter(pk=job.pk).update(status=Job.DONE, finished_at=now, last_error='', lease_expires=None,
                                         progress=progress if progress is not None else job.progress,
                                         duration_ms=max(0, int((now - (job.started_at or now)).total_seconds() * 1000)))


def fail(job, error, permanent=False):
    now = timezone.now()
    message = str(error)[:500]
    exhausted = permanent or job.attempts >= job.max_attempts
    Job.objects.filter(pk=job.pk).update(
        status=Job.FAILED if exhausted else Job.QUEUED, last_error=message, lease_expires=None,
        run_after=now if exhausted else now + backoff(job.attempts), finished_at=now if exhausted else None,
        progress=job.progress, duration_ms=max(0, int((now - (job.started_at or now)).total_seconds() * 1000)))
    return exhausted


def recover_expired(now=None):
    """Re-queue jobs whose worker died; the job's saved progress lets the retry resume."""
    now = now or timezone.now()
    recovered = 0
    for job in Job.objects.filter(status=Job.LEASED, lease_expires__lt=now):
        exhausted = job.attempts >= job.max_attempts
        Job.objects.filter(pk=job.pk, status=Job.LEASED).update(
            status=Job.FAILED if exhausted else Job.QUEUED, lease_expires=None,
            last_error='Worker lease expired; the job was interrupted.' + ('' if exhausted else ' Retry queued.'),
            run_after=now + backoff(job.attempts))
        recovered += 1
    return recovered


def run_job(job):
    """Execute one leased job; exceptions become retries or permanent failures."""
    fn = HANDLERS.get(job.kind)
    if not fn:
        fail(job, f'No handler for job kind {job.kind}', permanent=True)
        return False
    try:
        result = fn(job)
        complete(job, progress=result if isinstance(result, dict) else None)
        return True
    except PermanentJobError as exc:
        fail(job, exc, permanent=True)
    except Exception as exc:  # isolation: one customer's failure never blocks the others
        logger.warning('Job %s (%s) failed: %s', job.pk, job.kind, type(exc).__name__)
        fail(job, getattr(exc, 'user_message', None) or safe_message(exc))
    return False


def safe_message(exc):
    from .collector import safe_error
    return safe_error(exc)


def run_once(worker=None, now=None, limit=None, kinds=None):
    """Lease and run jobs until the queue is empty (or ``limit`` jobs ran). Used by tests and cron."""
    worker = worker or f'{socket.gethostname()}-{uuid.uuid4().hex[:6]}'
    ran = 0
    while limit is None or ran < limit:
        job = lease(worker, now=now, kinds=kinds)
        if not job:
            break
        run_job(job)
        ran += 1
    return ran


def work(concurrency=None, poll_seconds=None, stop_event=None, tick=None):
    """Supervised worker loop with bounded concurrency and periodic scheduling ticks."""
    from . import scheduler
    concurrency = concurrency or setting('WORKER_CONCURRENCY', 3)
    poll_seconds = poll_seconds or setting('WORKER_POLL_SECONDS', 5)
    stop_event = stop_event or threading.Event()
    worker_name = f'{socket.gethostname()}-{uuid.uuid4().hex[:6]}'

    def loop(index):
        name = f'{worker_name}-{index}'
        while not stop_event.is_set():
            try:
                close_old_connections()
                job = lease(name)
                if job:
                    run_job(job)
                    continue
            except Exception:
                logger.exception('Worker thread %s error', name)
            stop_event.wait(poll_seconds + random.uniform(0, 1))

    threads = [threading.Thread(target=loop, args=(i,), daemon=True, name=f'worker-{i}') for i in range(concurrency)]
    for thread in threads:
        thread.start()
    last_tick = 0
    try:
        while not stop_event.is_set():
            if time.monotonic() - last_tick >= setting('WORKER_TICK_SECONDS', 30):
                try:
                    close_old_connections()
                    recover_expired()
                    scheduler.schedule_due()
                    if tick:
                        tick()
                except Exception:
                    logger.exception('Scheduler tick failed')
                last_tick = time.monotonic()
            stop_event.wait(1)
    finally:
        stop_event.set()
        for thread in threads:
            thread.join(timeout=setting('JOB_LEASE_SECONDS', 600))
