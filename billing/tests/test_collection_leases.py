from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings

from billing import jobs
from billing.aws import Meter
from billing.collector import ConnectionChanged, collect_source, import_budgets
from billing.models import CollectionPeriod, Cost, ImportedBudget, Job, SyncRun
from .helpers import FakeSession, ce_page, cost, make_customer


@override_settings(REQUIRE_CONNECTION_APPROVAL=False, JOB_LEASE_SECONDS=60)
class CollectionLeaseTests(TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 18, 12, tzinfo=dt_timezone.utc)
        self.started = self.now
        clock = patch('django.utils.timezone.now', side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        _, self.source = make_customer('Paged account', '111111111111', kind='standalone')
        self.month = date(2026, 9, 1)
        self.previous_cost = cost(self.source, self.month, '99')
        self.previous_budget = ImportedBudget.objects.create(source=self.source, owning_account_id=self.source.account_id,
            name='Previous budget', budget_type='COST', time_unit='MONTHLY', limit_amount=100, limit_unit='USD')

    def lease(self, kind):
        jobs.enqueue(kind, key='paged-'+kind, source=self.source)
        return jobs.lease('page-worker')

    def cost_page(self, index, token=None):
        return ce_page([(self.source.account_id, f'Service {index}', str(index))], self.month, token=token)

    def budget_page(self, index, token=None):
        page = {'Budgets': [{'BudgetName': f'Budget {index}', 'BudgetType': 'COST', 'TimeUnit': 'MONTHLY',
                            'BudgetLimit': {'Amount': str(index * 100), 'Unit': 'USD'}}]}
        if token:
            page['NextToken'] = token
        return page

    def collect(self, client, job):
        return collect_source(self.source, months=[self.month], client=client, meter=Meter(limit=0),
                              today=self.started.date(), job=job)

    def budgets(self, client, job):
        return import_budgets(self.source, session=FakeSession(budgets=client), meter=Meter(limit=0), job=job)

    def test_cost_pages_renew_lease_until_full_month_can_publish(self):
        job = self.lease('collect')
        client = Mock()
        def page(**kwargs):
            index = int(kwargs.get('NextPageToken', '1'))
            self.now += timedelta(seconds=40)
            return self.cost_page(index, str(index + 1) if index < 3 else None)
        client.get_cost_and_usage.side_effect = page
        result = self.collect(client, job)
        self.assertEqual(result.status, 'success')
        self.assertEqual(result.requests, 3)
        self.assertEqual(self.now - self.started, timedelta(seconds=120))
        self.assertEqual(list(Cost.objects.filter(source=self.source).order_by('service').values_list('unblended', flat=True)),
                         [Decimal(1), Decimal(2), Decimal(3)])
        job.refresh_from_db()
        self.assertGreater(job.lease_expires, self.now)
        self.assertEqual(job.progress['completed'], [str(self.month)])

    def test_budget_pages_renew_lease_until_snapshot_can_publish(self):
        job = self.lease('import_budgets')
        client = Mock()
        def page(**kwargs):
            index = int(kwargs.get('NextToken', '1'))
            self.now += timedelta(seconds=40)
            return self.budget_page(index, str(index + 1) if index < 3 else None)
        client.describe_budgets.side_effect = page
        self.assertEqual(self.budgets(client, job), 3)
        self.assertEqual(self.now - self.started, timedelta(seconds=120))
        self.assertEqual(set(ImportedBudget.objects.filter(source=self.source).values_list('name', flat=True)),
                         {'Budget 1', 'Budget 2', 'Budget 3'})
        job.refresh_from_db()
        self.assertGreater(job.lease_expires, self.now)

    def replace_lease(self, job):
        self.now += timedelta(seconds=61)
        self.assertEqual(jobs.recover_expired(), 1)
        Job.objects.filter(pk=job.pk).update(run_after=self.now)
        replacement = jobs.lease('replacement-worker')
        self.assertEqual(replacement.pk, job.pk)
        return replacement

    def assert_replacement_preserved(self, job, replacement):
        current = Job.objects.get(pk=job.pk)
        self.assertEqual((current.worker, current.attempts, current.lease_expires),
                         (replacement.worker, replacement.attempts, replacement.lease_expires))
        self.assertEqual(current.status, Job.LEASED)

    def test_replaced_cost_worker_stops_before_next_page_and_keeps_snapshot(self):
        job = self.lease('collect')
        client, replacements = Mock(), []
        def page(**kwargs):
            replacements.append(self.replace_lease(job))
            Cost.objects.filter(pk=self.previous_cost.pk).update(unblended=150)
            return self.cost_page(1, '2')
        client.get_cost_and_usage.side_effect = page
        with self.assertRaises(ConnectionChanged):
            self.collect(client, job)
        self.assertEqual(client.get_cost_and_usage.call_count, 1)
        self.assertEqual(Cost.objects.get(source=self.source).unblended, Decimal(150))
        self.assert_replacement_preserved(job, replacements[0])

    def test_replaced_cost_worker_preserves_replacement_success_status(self):
        job = self.lease('collect')
        client = Mock()
        def page(**kwargs):
            replacement = self.replace_lease(job)
            fresh_client = Mock()
            fresh_client.get_cost_and_usage.return_value = self.cost_page(2)
            self.assertEqual(self.collect(fresh_client, replacement).status, 'success')
            jobs.complete(replacement)
            return self.cost_page(1, '2')
        client.get_cost_and_usage.side_effect = page
        with self.assertRaises(ConnectionChanged):
            self.collect(client, job)
        self.source.refresh_from_db()
        period = CollectionPeriod.objects.get(source=self.source, month=self.month)
        self.assertEqual(period.status, 'complete')
        self.assertEqual(period.last_error, '')
        self.assertEqual(period.attempts, 1)
        self.assertEqual(self.source.last_error, '')
        self.assertEqual(Cost.objects.get(source=self.source).unblended, Decimal(2))
        self.assertEqual(set(SyncRun.objects.filter(source=self.source).values_list('status', flat=True)), {'success', 'failed'})
        self.assertEqual(Job.objects.get(pk=job.pk).status, Job.DONE)

    def test_inactive_customer_failure_keeps_current_status(self):
        job = self.lease('collect')
        period = CollectionPeriod.objects.create(source=self.source, month=self.month, status='complete', last_error='Previous status')
        client = Mock()
        def page(**kwargs):
            self.source.customer.active = False
            self.source.customer.save(update_fields=['active'])
            type(self.source).objects.filter(pk=self.source.pk).update(last_error='Customer offboarded')
            return self.cost_page(1)
        client.get_cost_and_usage.side_effect = page
        with self.assertRaises(ConnectionChanged):
            self.collect(client, job)
        self.source.refresh_from_db()
        period.refresh_from_db()
        self.assertEqual((period.status, period.last_error), ('complete', 'Previous status'))
        self.assertEqual(self.source.last_error, 'Customer offboarded')
        self.assertEqual(Cost.objects.get(source=self.source).unblended, Decimal(99))

    def test_replaced_budget_worker_stops_before_next_page_and_keeps_snapshot(self):
        job = self.lease('import_budgets')
        client, replacements = Mock(), []
        def page(**kwargs):
            replacements.append(self.replace_lease(job))
            ImportedBudget.objects.filter(pk=self.previous_budget.pk).update(limit_amount=150)
            return self.budget_page(1, '2')
        client.describe_budgets.side_effect = page
        with self.assertRaises(ConnectionChanged):
            self.budgets(client, job)
        self.assertEqual(client.describe_budgets.call_count, 1)
        self.assertEqual(ImportedBudget.objects.get(source=self.source).limit_amount, Decimal(150))
        self.assert_replacement_preserved(job, replacements[0])

    def test_cost_response_cannot_revive_an_expired_lease(self):
        job = self.lease('collect')
        original_expiry = job.lease_expires
        client = Mock()
        def page(**kwargs):
            self.now += timedelta(seconds=61)
            return self.cost_page(1, '2')
        client.get_cost_and_usage.side_effect = page
        with self.assertRaises(ConnectionChanged):
            self.collect(client, job)
        self.assertEqual(client.get_cost_and_usage.call_count, 1)
        self.assertEqual(Cost.objects.get(source=self.source).pk, self.previous_cost.pk)
        job.refresh_from_db()
        self.assertEqual(job.lease_expires, original_expiry)

    def test_budget_response_cannot_revive_an_expired_lease(self):
        job = self.lease('import_budgets')
        original_expiry = job.lease_expires
        client = Mock()
        def page(**kwargs):
            self.now += timedelta(seconds=61)
            return self.budget_page(1, '2')
        client.describe_budgets.side_effect = page
        with self.assertRaises(ConnectionChanged):
            self.budgets(client, job)
        self.assertEqual(client.describe_budgets.call_count, 1)
        self.assertEqual(ImportedBudget.objects.get(source=self.source).pk, self.previous_budget.pk)
        job.refresh_from_db()
        self.assertEqual(job.lease_expires, original_expiry)
