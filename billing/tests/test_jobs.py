from django.test import override_settings
"""Durable job queue: coalescing, leases, crash recovery, backoff, fairness and scheduling."""
from datetime import datetime, timedelta, timezone as dt_tz
from unittest.mock import patch
from unittest import skipUnless
from concurrent.futures import ThreadPoolExecutor
from django.db import close_old_connections, connection, transaction
from django.core.exceptions import ValidationError
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

    def test_activation_verification_retries_iam_propagation_but_rejects_bad_trust(self):
        from botocore.exceptions import ClientError
        job, _ = jobs.enqueue('verify', key='activation:propagation', source=self.source, max_attempts=3,
                              payload={'activation_id': 'test-activation'})
        with patch('billing.scheduler.collector.verify_source', side_effect=ClientError({'Error': {'Code': 'AccessDenied'}}, 'AssumeRole')):
            jobs.run_job(jobs.lease('propagation-test'))
        job.refresh_from_db()
        self.assertEqual(job.status, Job.QUEUED)
        self.assertEqual(job.attempts, 1)
        Job.objects.filter(pk=job.pk).update(run_after=timezone.now())
        with patch('billing.scheduler.collector.verify_source', side_effect=ValueError('Unsafe customer trust')):
            jobs.run_job(jobs.lease('trust-test'))
        job.refresh_from_db()
        self.assertEqual(job.status, Job.FAILED)
        self.assertIn('Unsafe customer trust', job.last_error)

    def test_activation_waits_for_selected_budget_permission_to_propagate(self):
        self.source.approved_capabilities = ['budgets']
        self.source.save(update_fields=['approved_capabilities'])
        job, _ = jobs.enqueue('verify', key='activation:budget-propagation', source=self.source, max_attempts=3,
                              payload={'activation_id': 'test-activation'})
        with patch('billing.scheduler.collector.verify_source', return_value={'cost_explorer': True, 'budgets': False, 'budgets_error': 'AccessDeniedException'}):
            jobs.run_job(jobs.lease('budget-propagation-test'))
        job.refresh_from_db()
        self.source.refresh_from_db()
        self.assertEqual(job.status, Job.QUEUED)
        self.assertIsNone(self.source.verified_at)
        self.assertFalse(Job.objects.filter(kind='discover').exists())

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
        BillingSource.objects.update(last_success=now - timedelta(days=1))
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
        Job.objects.update(status=Job.DONE)
        later = scheduler.schedule_due(datetime(2026, 10, 2, 0, 5, tzinfo=dt_tz.utc))
        self.assertGreater(later, 0)
        self.assertEqual(sum(1 for j in Job.objects.filter(kind='collect') if j.payload.get('months_back') == 6), 2)

    def test_manual_refresh_is_coalesced(self):
        scheduler.request_refresh(self.source)
        scheduler.request_refresh(self.source)
        self.assertEqual(Job.objects.filter(kind='collect', source=self.source).count(), 1)
        self.assertFalse(BillingSource.objects.get(pk=self.source.pk).sync_requested)

    @patch('django.utils.timezone.now', return_value=datetime(2026, 9, 8, 0, 10, tzinfo=dt_tz.utc))
    def test_manual_first_pull_coalesces_and_only_next_slot_collects_again(self, clock):
        now = timezone.now()
        self.source.initial_import_done = False
        self.source.last_success = None
        self.source.approved_capabilities = ['budgets']
        self.source.save()
        first, created = scheduler.request_initial_import(self.source, actor='operator', actor_id=7)
        repeated, repeated_created = scheduler.request_refresh(self.source, actor='operator', actor_id=7)
        self.assertTrue(created)
        self.assertFalse(repeated_created)
        self.assertEqual(first.pk, repeated.pk)
        self.assertEqual(Job.objects.filter(source=self.source).count(), 2)
        self.assertEqual(first.payload['months_back'], 6)
        self.assertEqual(first.payload['actor_id'], 7)
        self.assertEqual(first.payload['connection_version'], self.source.connection_version)
        scheduler.schedule_due(now)
        self.assertEqual(Job.objects.filter(source=self.source).count(), 2)
        Job.objects.filter(source=self.source).update(status=Job.DONE)
        BillingSource.objects.filter(pk=self.source.pk).update(initial_import_done=True, last_success=now,
            capabilities={'budgets_imported_at': now.isoformat()})
        scheduler.schedule_due(now + timedelta(seconds=1))
        self.assertEqual(Job.objects.filter(source=self.source).count(), 2)
        later = scheduler.next_slot(now)
        scheduler.schedule_due(later)
        scheduler.schedule_due(later + timedelta(minutes=20))
        self.assertEqual(Job.objects.filter(source=self.source, kind='collect').count(), 2)
        self.assertEqual(Job.objects.filter(source=self.source, kind='import_budgets').count(), 2)
        self.source.refresh_from_db()
        self.assertGreater(self.source.next_run, later + timedelta(minutes=20))

    def test_new_accounts_pull_automatically_without_slot_delay_or_extra_discovery(self):
        now = datetime(2026, 9, 8, 0, 1, tzinfo=dt_tz.utc)
        BillingSource.objects.filter(pk=self.source.pk).update(initial_import_done=False, last_success=None,
            approved_capabilities=['budgets'])
        with self.settings(SCHEDULE_JITTER_SECONDS=900):
            scheduler.schedule_due(now)
            scheduler.schedule_due(now + timedelta(seconds=30))
        account_jobs = Job.objects.filter(source=self.source)
        self.assertCountEqual(account_jobs.values_list('kind', flat=True), ['collect', 'import_budgets'])
        for job in account_jobs:
            self.assertEqual(job.run_after, now)
            self.assertEqual(job.payload['initial'], True)
            self.assertEqual(job.payload['connection_version'], self.source.connection_version)
        first = account_jobs.get(kind='collect')
        self.assertEqual(first.payload['months_back'], 6)
        repeated, created = scheduler.request_refresh(self.source)
        self.assertEqual(repeated.pk, first.pk)
        self.assertFalse(created)
        self.assertEqual(account_jobs.count(), 2)
        # Import completion in the same slot must not create a second, regular pull.
        account_jobs.update(status=Job.DONE)
        BillingSource.objects.filter(pk=self.source.pk).update(initial_import_done=True, last_success=now,
            capabilities={'budgets_imported_at': now.isoformat()})
        scheduler.schedule_due(now + timedelta(minutes=1))
        self.assertEqual(account_jobs.count(), 2)

    def test_new_budget_reader_pulls_automatically_and_legacy_readers_continue(self):
        self.source.kind = BillingSource.MEMBER_BUDGETS
        self.source.approved_capabilities = ['budgets']
        self.source.initial_import_done = False
        self.source.last_success = None
        self.source.discovered_at = None
        self.source.save()
        now = timezone.now()
        scheduler.schedule_due(now)
        first = Job.objects.get(source=self.source)
        self.assertEqual(first.kind, 'import_budgets')
        self.assertEqual(first.run_after, now)
        repeated, created = scheduler.request_refresh(self.source)
        self.assertEqual(repeated.pk, first.pk)
        self.assertFalse(created)
        self.assertFalse(Job.objects.filter(source=self.source, kind='collect').exists())
        with patch('billing.scheduler.collector.import_budgets', return_value=0):
            scheduler.handle_import_budgets(first)
        self.source.refresh_from_db()
        self.assertTrue(scheduler.collection_initialized(self.source))
        self.assertIsNotNone(self.source.last_success)
        Job.objects.filter(source=self.source).delete()
        BillingSource.objects.filter(pk=self.source.pk).update(initial_import_done=False, last_success=now - timedelta(days=1), capabilities={})
        scheduler.schedule_due(now)
        self.assertEqual(Job.objects.filter(source=self.source, kind='import_budgets').count(), 1)

    @patch('django.utils.timezone.now', return_value=datetime(2026, 9, 8, 0, 10, tzinfo=dt_tz.utc))
    def test_automatic_first_pull_preserves_backoff_and_recovers_in_a_later_slot(self, clock):
        now = timezone.now()
        BillingSource.objects.filter(pk=self.source.pk).update(initial_import_done=False, last_success=None)
        scheduler.schedule_due(now)
        first = Job.objects.get(source=self.source, kind='collect')
        Job.objects.filter(pk=first.pk).update(max_attempts=2)
        with patch('billing.scheduler.Session'), patch('billing.scheduler.collector.collect_source', side_effect=RuntimeError('Temporary outage')):
            self.assertFalse(jobs.run_job(jobs.lease('first-attempt', now=now, kinds=['collect'])))
            first.refresh_from_db()
            self.assertEqual(first.status, Job.QUEUED)
            retry_at = first.run_after
            scheduler.schedule_due(now + timedelta(seconds=30))
            first.refresh_from_db()
            self.assertEqual(first.run_after, retry_at)
            self.assertEqual(first.attempts, 1)
            self.assertEqual(Job.objects.filter(source=self.source, kind='collect').count(), 1)
            self.assertFalse(jobs.run_job(jobs.lease('last-attempt', now=retry_at, kinds=['collect'])))
        first.refresh_from_db()
        self.assertEqual(first.status, Job.FAILED)
        for _ in range(3):
            scheduler.schedule_due(now + timedelta(minutes=5))
        self.assertEqual(Job.objects.filter(source=self.source, kind='collect').count(), 1)
        later = scheduler.next_slot(now)
        scheduler.schedule_due(later)
        scheduler.schedule_due(later + timedelta(seconds=30))
        fresh = Job.objects.get(source=self.source, kind='collect', status=Job.QUEUED)
        self.assertEqual(fresh.run_after, later)
        self.assertEqual(fresh.attempts, 0)
        self.assertEqual(fresh.payload['months_back'], 6)
        self.assertNotEqual(fresh.key, first.key)

    @patch('django.utils.timezone.now', return_value=datetime(2026, 9, 8, 0, 10, tzinfo=dt_tz.utc))
    def test_failed_manual_first_pull_does_not_restart_on_every_scheduler_tick(self, clock):
        now = timezone.now()
        BillingSource.objects.filter(pk=self.source.pk).update(initial_import_done=False, last_success=None)
        first, _ = scheduler.request_initial_import(self.source)
        Job.objects.filter(pk=first.pk).update(status=Job.FAILED)
        scheduler.schedule_due(now)
        scheduler.schedule_due(now)
        self.assertEqual(Job.objects.filter(source=self.source, kind='collect').count(), 1)
        # An explicit dashboard retry remains available before the next slot.
        retried, made = scheduler.request_refresh(self.source)
        self.assertTrue(made)
        self.assertNotEqual(retried.pk, first.pk)

    def test_automatic_first_pull_waits_for_activation_and_other_readiness_guards(self):
        from django.contrib.auth.models import User
        from billing.models import ActivationRequest
        now = timezone.now()
        BillingSource.objects.filter(pk=self.source.pk).update(initial_import_done=False, last_success=None)
        user = User.objects.create_user('connecting-operator')
        activation = ActivationRequest.objects.create(source=self.source, requested_by=user, session_version=1,
            connection_version=self.source.connection_version, snapshot={})
        for status in ('queued', 'processing', 'verifying', 'importing'):
            ActivationRequest.objects.filter(pk=activation.pk).update(status=status)
            scheduler.schedule_due(now)
            self.assertFalse(Job.objects.filter(source=self.source).exists())
        ActivationRequest.objects.filter(pk=activation.pk).update(status='completed')
        for changes in ({'enabled': False}, {'verified_at': None}, {'discovered_at': None}, {'role_arn': ''}):
            with self.subTest(changes=changes):
                BillingSource.objects.filter(pk=self.source.pk).update(enabled=True, verified_at=now, discovered_at=now,
                    role_arn=self.source.role_arn)
                BillingSource.objects.filter(pk=self.source.pk).update(**changes)
                scheduler.schedule_due(now)
                self.assertFalse(Job.objects.filter(source=self.source).exists())
        BillingSource.objects.filter(pk=self.source.pk).update(role_arn=self.source.role_arn)
        self.customer.active = False
        self.customer.save()
        scheduler.schedule_due(now)
        self.assertFalse(Job.objects.filter(source=self.source).exists())
        self.customer.active = True
        self.customer.save()
        scheduler.schedule_due(now)
        self.assertTrue(Job.objects.filter(source=self.source, kind='collect').exists())

    def test_budget_reader_without_budget_approval_does_not_start(self):
        BillingSource.objects.filter(pk=self.source.pk).update(kind=BillingSource.MEMBER_BUDGETS,
            initial_import_done=False, last_success=None, discovered_at=None, approved_capabilities=[], capabilities={})
        scheduler.schedule_due()
        self.assertFalse(Job.objects.filter(source=self.source).exists())

    def test_first_connection_verification_does_not_import_budgets(self):
        self.source.initial_import_done = False
        self.source.last_success = None
        self.source.approved_capabilities = ['budgets']
        self.source.save()
        with patch('billing.scheduler.collector.verify_source', return_value={'budgets': True}):
            scheduler.handle_verify(Job(source=self.source))
        self.assertTrue(Job.objects.filter(source=self.source, kind='discover').exists())
        self.assertFalse(Job.objects.filter(source=self.source, kind__in=['collect', 'import_budgets']).exists())

    def test_paused_unverified_and_inactive_sources_cannot_queue_manual_pull(self):
        for changes in ({'enabled': False}, {'verified_at': None}, {'discovered_at': None}):
            with self.subTest(changes=changes):
                BillingSource.objects.filter(pk=self.source.pk).update(enabled=True, verified_at=timezone.now(), discovered_at=timezone.now())
                BillingSource.objects.filter(pk=self.source.pk).update(**changes)
                with self.assertRaises(ValidationError):
                    scheduler.request_refresh(self.source)
                self.assertFalse(Job.objects.filter(source=self.source).exists())
        BillingSource.objects.filter(pk=self.source.pk).update(verified_at=timezone.now(), discovered_at=timezone.now())
        self.customer.active = False
        self.customer.save()
        with self.assertRaises(ValidationError):
            scheduler.request_initial_import(self.source)

    @override_settings(REQUIRE_CONNECTION_APPROVAL=True)
    def test_revoked_account_skipped_without_blocking_approved_customer(self):
        from billing.models import CustomerApproval, RoleApproval
        from django.conf import settings
        now = timezone.now()
        for source in (self.source, self.other_source):
            CustomerApproval.objects.create(customer=source.customer, contacts=['Finance'], authorized_users=['operator'],
                expected_accounts=[source.account_id], billing_fields=['cost'], storage_region=settings.AWS_REGION,
                retention_days=365, status='approved', evidence='Authorized fixture', approved_by='operator', approved_at=now)
            RoleApproval.objects.create(source=source, role_arn=source.role_arn, connection_version=source.connection_version,
                status='approved', requested_by='operator', evidence='Authorized role', approved_at=now)
            checks = {'correct_external_id': 'passed', 'missing_external_id': 'denied', 'wrong_external_id': 'denied',
                      'account_identity': 'passed', 'exact_collector_principal': 'passed', 'connection_version': source.connection_version}
            BillingSource.objects.filter(pk=source.pk).update(initial_import_done=False, last_success=None, trust_checks=checks)
        RoleApproval.objects.filter(source=self.source).update(status='revoked')
        scheduler.schedule_due(now)
        self.assertFalse(Job.objects.filter(source=self.source).exists())
        self.assertTrue(Job.objects.filter(source=self.other_source, kind='collect').exists())
        with self.assertRaises(ValidationError):
            scheduler.request_refresh(self.source)

    def test_scheduler_queue_failure_does_not_block_other_source_and_recovers(self):
        now = timezone.now()
        BillingSource.objects.update(initial_import_done=False, last_success=None)
        real_enqueue = jobs.enqueue
        def enqueue(kind, **kwargs):
            if kwargs.get('source') == self.source:
                raise RuntimeError('Isolated source failure')
            return real_enqueue(kind, **kwargs)
        with patch('billing.scheduler.jobs.enqueue', side_effect=enqueue):
            scheduler.schedule_due(now)
        self.assertFalse(Job.objects.filter(source=self.source).exists())
        self.assertEqual(Job.objects.filter(source=self.other_source, kind='collect').count(), 1)
        scheduler.schedule_due(now)
        scheduler.schedule_due(now)
        self.assertEqual(Job.objects.filter(source=self.source, kind='collect').count(), 1)
        self.assertEqual(Job.objects.filter(source=self.other_source, kind='collect').count(), 1)

    def test_stale_version_job_fails_before_aws_and_current_request_can_queue(self):
        first, _ = scheduler.request_refresh(self.source)
        BillingSource.objects.filter(pk=self.source.pk).update(connection_version=2)
        second, created = scheduler.request_refresh(self.source)
        self.assertTrue(created)
        self.assertNotEqual(first.pk, second.pk)
        with patch('billing.scheduler.Session') as session:
            self.assertFalse(jobs.run_job(jobs.lease('old-version')))
            session.assert_not_called()
        first.refresh_from_db()
        self.assertEqual(first.status, Job.FAILED)
        self.assertEqual(second.payload['connection_version'], 2)

    def assert_current_setup_job_survives_stale_request(self, kind, request, operation, result):
        first, _ = request(self.source)
        BillingSource.objects.filter(pk=self.source.pk).update(connection_version=2)
        self.source.refresh_from_db()
        second, created = request(self.source)
        repeated, created_again = request(self.source)
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(second.pk, repeated.pk)
        with patch(operation, return_value=result) as call:
            self.assertEqual(jobs.run_once(kinds=[kind]), 2)
            call.assert_called_once()
            self.assertEqual(call.call_args.args[0].connection_version, 2)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.status, Job.FAILED)
        self.assertEqual(second.status, Job.DONE)

    def test_current_verification_is_not_coalesced_into_stale_version(self):
        self.assert_current_setup_job_survives_stale_request('verify', scheduler.request_verification,
            'billing.scheduler.collector.verify_source', {'cost_explorer': True})

    def test_current_discovery_is_not_coalesced_into_stale_version(self):
        self.assert_current_setup_job_survives_stale_request('discover', scheduler.request_discovery,
            'billing.scheduler.collector.discover_accounts', [])

    def test_reverification_queues_budget_import_for_the_current_version(self):
        with patch('billing.scheduler.collector.verify_source', return_value={'budgets': True}):
            scheduler.handle_verify(Job(source=self.source, kind='verify'))
            BillingSource.objects.filter(pk=self.source.pk).update(connection_version=2)
            scheduler.handle_verify(Job(source=self.source, kind='verify'))
            scheduler.handle_verify(Job(source=self.source, kind='verify'))
        imports = list(Job.objects.filter(source=self.source, kind='import_budgets').order_by('pk'))
        self.assertEqual(len(imports), 2)
        self.assertEqual([job.payload['connection_version'] for job in imports], [1, 2])
        with self.assertRaises(jobs.PermanentJobError):
            scheduler.load_source(imports[0])
        self.assertEqual(scheduler.load_source(imports[1]).connection_version, 2)

    def test_heartbeat_cannot_resurrect_expired_lease(self):
        jobs.enqueue('collect', source=self.source)
        job = jobs.lease('expired-worker')
        expired = timezone.now() - timedelta(seconds=1)
        Job.objects.filter(pk=job.pk).update(lease_expires=expired)
        self.assertFalse(jobs.heartbeat(job, {'completed': ['stale']}))
        job.refresh_from_db()
        self.assertEqual(job.lease_expires, expired)
        self.assertEqual(job.progress, {})

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
