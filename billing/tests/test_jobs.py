from django.test import override_settings
"""Durable job queue: coalescing, leases, crash recovery, backoff, fairness and scheduling."""
from datetime import datetime, timedelta, timezone as dt_tz
from unittest.mock import patch
from unittest import skipUnless
from concurrent.futures import ThreadPoolExecutor
from django.db import close_old_connections, connection, transaction
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from billing import jobs, scheduler
from billing.models import BillingSource, Job
from .helpers import make_customer


@skipUnless(connection.vendor == 'postgresql', 'Requires PostgreSQL row locking')
class ConcurrentLeaseTests(TransactionTestCase):
    def test_claim_skips_source_locked_by_another_transaction(self):
        _, source = make_customer('Locked', '111111111111')
        _, other = make_customer('Available', '222222222222')
        blocked, _ = jobs.enqueue('collect', key='locked', source=source)
        available, _ = jobs.enqueue('collect', key='available', source=other)

        def claim():
            close_old_connections()
            try:
                job = jobs.lease('concurrent-worker')
                return job.pk if job else None
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                BillingSource.objects.select_for_update().get(pk=source.pk)
                self.assertEqual(pool.submit(claim).result(timeout=10), available.pk)
        blocked.refresh_from_db()
        self.assertEqual(blocked.status, Job.QUEUED)
        self.assertEqual(jobs.lease('next-worker').pk, blocked.pk)


@override_settings(REQUIRE_CONNECTION_APPROVAL=False)
class JobQueueTests(TestCase):
    def setUp(self):
        self.customer, self.source = make_customer('One', '111111111111')
        self.other, self.other_source = make_customer('Two', '222222222222')

    def test_enqueue_coalesces_duplicate_requests(self):
        first, created = jobs.enqueue('collect', key='collect:x', source=self.source)
        second, created_again = jobs.enqueue('collect', key='collect:x', source=self.source, run_after=timezone.now() - timedelta(minutes=5))
        self.assertTrue(created); self.assertFalse(created_again); self.assertEqual(first.pk, second.pk)
        self.assertEqual(Job.objects.count(), 1)
        first=jobs.lease('coalescing-test')
        jobs.complete(first)
        third, created_third = jobs.enqueue('collect', key='collect:x', source=self.source)
        self.assertTrue(created_third)
        _, made = jobs.enqueue('collect', key='collect:x', once=True)
        self.assertFalse(made)

    def test_lease_excludes_busy_sources_and_respects_run_after(self):
        a, _ = jobs.enqueue('a', key='a1', source=self.source)
        b, _ = jobs.enqueue('a', key='a2', source=self.source)
        c, _ = jobs.enqueue('a', key='c1', source=self.other_source)
        future, _ = jobs.enqueue('a', key='f', run_after=timezone.now() + timedelta(hours=1))
        first = jobs.lease('w1')
        second = jobs.lease('w2')
        self.assertEqual(first.pk, a.pk)
        self.assertEqual(second.pk, c.pk)  # per-source exclusion skipped a2 while a1 is leased
        self.assertIsNone(jobs.lease('w3'))
        jobs.complete(first)
        self.assertEqual(jobs.lease('w3').pk, b.pk)

    def test_failure_backoff_then_permanent_failure(self):
        job, _ = jobs.enqueue('a', key='k', max_attempts=2)

        @jobs.handler('a')
        def boom(job):
            raise RuntimeError('nope')
        try:
            self.assertFalse(jobs.run_job(jobs.lease('w')))
            job.refresh_from_db()
            self.assertEqual(job.status, Job.QUEUED)
            self.assertGreater(job.run_after, timezone.now())
            self.assertEqual(job.attempts, 1)
            Job.objects.filter(pk=job.pk).update(run_after=timezone.now())
            jobs.run_job(jobs.lease('w'))
            job.refresh_from_db()
            self.assertEqual(job.status, Job.FAILED)
            self.assertIn('Collection failed', job.last_error)
        finally:
            jobs.HANDLERS.pop('a', None)

    def test_permanent_error_does_not_retry(self):
        @jobs.handler('perm')
        def perm(job):
            raise jobs.PermanentJobError('no role')
        try:
            jobs.enqueue('perm', key='p')
            jobs.run_once()
            job = Job.objects.get(key='p')
            self.assertEqual((job.status, job.last_error), (Job.FAILED, 'no role'))
        finally:
            jobs.HANDLERS.pop('perm', None)

    def test_crash_recovery_requeues_expired_leases_with_progress(self):
        job, _ = jobs.enqueue('collect', key='c', source=self.source)
        leased = jobs.lease('dead-worker')
        jobs.heartbeat(leased, {'completed': ['2026-07-01']})
        Job.objects.filter(pk=job.pk).update(lease_expires=timezone.now() - timedelta(seconds=1))
        self.assertEqual(jobs.recover_expired(), 1)
        job.refresh_from_db()
        self.assertEqual(job.status, Job.QUEUED)
        self.assertEqual(job.progress, {'completed': ['2026-07-01']})
        self.assertIn('interrupted', job.last_error)

    def test_isolated_failures_do_not_block_other_jobs(self):
        results = []

        @jobs.handler('ok')
        def ok(job):
            results.append(job.key)

        @jobs.handler('bad')
        def bad(job):
            raise RuntimeError('boom')
        try:
            jobs.enqueue('bad', key='bad1', source=self.source, priority=1)
            jobs.enqueue('ok', key='ok1', source=self.other_source, priority=5)
            jobs.enqueue('ok', key='ok2', priority=5)
            self.assertEqual(jobs.run_once(), 3)
            self.assertEqual(sorted(results), ['ok1', 'ok2'])
        finally:
            jobs.HANDLERS.pop('ok', None); jobs.HANDLERS.pop('bad', None)

    def test_schedule_due_is_idempotent_and_jittered(self):
        now = datetime(2026, 9, 8, 6, 20, tzinfo=dt_tz.utc)
        with self.settings(SCHEDULE_JITTER_SECONDS=900):
            created = scheduler.schedule_due(now)
            self.assertGreater(created, 0)
            again = scheduler.schedule_due(now)
        self.assertEqual(again, 0)
        collects = Job.objects.filter(kind='collect')
        self.assertEqual(collects.count(), 2)
        for job in collects:
            self.assertGreaterEqual(job.run_after, datetime(2026, 9, 8, 6, 0, tzinfo=dt_tz.utc))
            self.assertLess(job.run_after, datetime(2026, 9, 8, 6, 15, tzinfo=dt_tz.utc))
            self.assertEqual(job.payload['months_back'], 1)
        self.assertTrue(Job.objects.filter(kind='evaluate_budgets').exists())
        self.assertTrue(Job.objects.filter(kind='allocate_projects').exists())
        # the next slot creates new keys; the reconciliation day requests full history
        later = scheduler.schedule_due(datetime(2026, 10, 2, 0, 5, tzinfo=dt_tz.utc))
        self.assertGreater(later, 0)
        self.assertEqual(sum(1 for j in Job.objects.filter(kind='collect') if j.payload.get('months_back') == 6), 2)

    def test_manual_refresh_is_coalesced(self):
        scheduler.request_refresh(self.source)
        scheduler.request_refresh(self.source)
        self.assertEqual(Job.objects.filter(kind='collect', source=self.source).count(), 1)
        self.assertTrue(BillingSource.objects.get(pk=self.source.pk).sync_requested)

    def test_paused_source_jobs_fail_permanently(self):
        BillingSource.objects.filter(pk=self.source.pk).update(enabled=False)
        jobs.enqueue('collect', key='c', source=self.source)
        jobs.run_once()
        job = Job.objects.get(key='c')
        self.assertEqual(job.status, Job.FAILED)
        self.assertIn('paused', job.last_error)

    def test_collect_handler_uses_session_and_records_progress(self):
        from datetime import date
        from .helpers import ce_client, ce_page, FakeSession
        pages = [ce_page([('111111111111', 'Amazon EC2', '1')], date(2026, 8, 1)), ce_page([('111111111111', 'Amazon EC2', '2')], date(2026, 9, 1))]
        with patch('billing.scheduler.Session', return_value=FakeSession(ce=ce_client(pages))), patch('billing.scheduler.timezone.now', return_value=datetime(2026, 9, 8, 12, tzinfo=dt_tz.utc)):
            jobs.enqueue('collect', key='c', source=self.source, payload={'months_back': 1})
            jobs.run_once()
        job = Job.objects.get(key='c')
        self.assertEqual(job.status, Job.DONE)
        self.assertEqual(job.progress['completed'], ['2026-08-01', '2026-09-01'])
        self.assertEqual(job.progress['requests'], 2)
