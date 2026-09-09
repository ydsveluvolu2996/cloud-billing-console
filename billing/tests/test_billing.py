"""Regression tests for collection, reporting, exports and access control on the new data model."""
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import Mock, patch
import yaml
from botocore.exceptions import ClientError
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings, Client
from django.utils import timezone
from billing.aws import Meter
from billing.collector import collect_source, verify_source
from billing.models import Budget, BudgetAmount, CollectionPeriod, Cost, Customer, BillingSource
from billing.onboarding import source_template, quick_create_url
from billing.reporting import report
from .helpers import FakeSession, assign, ce_client, ce_page, cost, make_customer, web_settings


@web_settings
class BillingTests(TestCase):
    def setUp(self):
        self.customer, self.source = make_customer('Test customer', '123456789012')
        self.admin = User.objects.create_user('owner', password='test-only-a-long-password', is_staff=True)
        self.reader = User.objects.create_user('reader', password='test-only-a-long-password')
        self.today = date(2026, 9, 8)

    def collect(self, pages, months=None):
        return collect_source(self.source, months=months or [date(2026, 9, 1)], client=ce_client(pages), meter=Meter(limit=0), today=self.today)

    def test_sync_replaces_revisions_without_duplicates(self):
        for amount in ('10.10', '11.25'):
            run = self.collect([ce_page([('123456789012', 'Amazon EC2', amount)], date(2026, 9, 1))])
            self.assertEqual(run.status, 'success')
        self.assertEqual(Cost.objects.count(), 1)
        self.assertEqual(Cost.objects.get().unblended, Decimal('11.25'))
        period = CollectionPeriod.objects.get(source=self.source, month=date(2026, 9, 1))
        self.assertEqual((period.status, period.revision, period.rows), ('complete', 2, 1))

    def test_failure_on_later_page_preserves_all_previous_data(self):
        old = cost(self.source, date(2026, 9, 1), '90')
        before = self.source.last_success
        with self.assertRaises(ClientError):
            self.collect([ce_page([('123456789012', 'Amazon EC2', '5')], date(2026, 9, 1), token='next'),
                          ClientError({'Error': {'Code': 'AccessDenied'}}, 'GetCostAndUsage')])
        self.assertEqual(Cost.objects.get(pk=old.pk).unblended, 90)
        self.source.refresh_from_db()
        self.assertEqual(self.source.last_success, before)
        self.assertIn('AccessDenied', self.source.last_error)
        self.assertEqual(self.source.syncs.first().status, 'failed')

    def test_pagination_and_negative_credits(self):
        client = ce_client([ce_page([('123456789012', 'Amazon EC2', '10.10')], date(2026, 9, 1), token='next'),
                            ce_page([('123456789012', 'Amazon EC2', '-2.50')], date(2026, 9, 2))])
        run = collect_source(self.source, months=[date(2026, 9, 1)], client=client, meter=Meter(limit=0), today=self.today)
        self.assertEqual(run.status, 'success')
        self.assertEqual(Cost.objects.count(), 2)
        self.assertEqual(client.get_cost_and_usage.call_args.kwargs['NextPageToken'], 'next')
        self.assertEqual(sum(Cost.objects.values_list('unblended', flat=True)), Decimal('7.60'))
        self.assertEqual(run.requests, 2)

    def test_paused_source_never_calls_aws(self):
        self.source.enabled = False
        client = Mock()
        self.assertIsNone(collect_source(self.source, client=client))
        client.get_cost_and_usage.assert_not_called()

    def test_role_must_match_account_and_restricted_path(self):
        self.source.role_arn = 'arn:aws:iam::999999999999:role/Admin'
        with self.assertRaises(ValidationError):
            self.source.full_clean()

    def test_overlapping_account_import_is_rejected(self):
        other, other_source = make_customer('Existing payer', '222222222222')
        cost(other_source, date(2026, 9, 1), '90', account_id='123456789012')
        with self.assertRaises(Exception):
            self.collect([ce_page([('123456789012', 'Amazon EC2', '5')], date(2026, 9, 1))])
        self.source.refresh_from_db()
        self.assertIn('already collected through another connection', self.source.last_error)
        self.assertEqual(Cost.objects.count(), 1)

    def test_management_account_costs_counted_once(self):
        """Payer row = management account's own spend + members; never payer + members again."""
        run = self.collect([ce_page([('123456789012', 'AWS Support', '30'), ('210987654321', 'Amazon EC2', '70'), ('210987654321', 'Amazon S3', '5')], date(2026, 9, 1))])
        self.assertEqual(run.status, 'success')
        result = report({'start': '2026-09-01', 'end': '2026-09-01', 'customer': str(self.customer.pk)})
        self.assertEqual(result['total'], Decimal('105'))
        self.assertEqual(len(result['account_rows']), 2)
        self.assertTrue(any(r['is_management'] for r in result['account_rows']))

    def test_budget_uses_whole_customer_despite_service_filter(self):
        today = timezone.now().date()
        cost(self.source, today, '10')
        cost(self.source, today, '20', service='Amazon S3')
        budget = Budget.objects.create(customer=self.customer, scope=Budget.CUSTOMER, name='b')
        BudgetAmount.objects.create(budget=budget, amount=Decimal('25'))
        result = report({'service': 'Amazon EC2'})
        self.assertEqual(result['total'], Decimal('10'))
        row = result['rows'][0]
        self.assertEqual(row['mtd'], Decimal('30'))
        self.assertTrue(row['over_budget'])
        self.assertEqual(result['over_budget'], 1)

    def test_missing_budget_is_not_configured_not_zero(self):
        cost(self.source, timezone.now().date(), '10')
        row = report({})['rows'][0]
        self.assertIsNone(row['budget'])
        self.assertFalse(row['over_budget'])
        self.assertEqual(row['budget_percent'], 0)

    def test_currencies_are_never_summed(self):
        cost(self.source, date(2026, 9, 1), '10')
        cost(self.source, date(2026, 9, 1), '10', currency='EUR', service='Amazon S3')
        self.assertEqual(report({'start': '2026-09-01', 'end': '2026-09-01'})['total'], Decimal('10'))
        self.assertEqual(report({'start': '2026-09-01', 'end': '2026-09-01', 'currency': 'EUR'})['total'], Decimal('10'))
        with self.assertRaises(ValueError):
            report({'currency': 'JPY'})

    def test_missing_versus_zero(self):
        cost(self.source, date(2026, 9, 1), '0')
        result = report({'start': '2026-09-01', 'end': '2026-09-02'})
        self.assertEqual(result['points'][0]['amount'], 0.0)
        self.assertIsNone(result['points'][1]['amount'])
        self.assertEqual(result['missing_days'], 1)

    def test_reader_cannot_manage_but_can_view_and_export(self):
        client = Client()
        client.force_login(self.reader)
        self.assertEqual(client.get('/customers/').status_code, 200)
        self.assertEqual(client.get('/customers/add/').status_code, 403)
        self.assertEqual(client.post(f'/customers/{self.customer.pk}/sync/').status_code, 403)
        self.assertEqual(client.get(f'/sources/{self.source.pk}/').status_code, 403)
        self.assertEqual(client.get(f'/budgets/add/?customer={self.customer.pk}').status_code, 403)
        self.assertEqual(client.post(f'/sources/{self.source.pk}/pause/').status_code, 403)
        cost(self.source, date(2026, 9, 1), '1.5')
        response = client.get('/export/?start=2026-09-01&end=2026-09-01')
        self.assertContains(response, '1.5000000000')
        self.assertEqual(Client().get('/customers/').status_code, 302)

    def test_dashboard_and_portfolio_render_with_scope_links(self):
        client = Client()
        client.force_login(self.admin)
        cost(self.source, date(2026, 9, 1), '5')
        self.assertContains(client.get('/portfolio/?start=2026-09-01&end=2026-09-01'), 'Test customer')
        self.assertEqual(client.get('/').status_code, 200)
        self.assertEqual(client.get('/overview/').status_code, 200)
        self.assertEqual(client.get(f'/customers/{self.customer.pk}/').status_code, 200)
        self.assertEqual(client.get('/portfolio/?customer=not-a-uuid').status_code, 400)

    def test_manual_policy_bundle_uses_connection_identity(self):
        with override_settings(COLLECTOR_ROLE_ARN='arn:aws:iam::111111111111:role/Collector'):
            import json
            bundle = json.loads(source_template(self.source))
            self.assertEqual(bundle['external_id'], self.source.external_id)
            trust = bundle['trust_policy']['Statement'][0]
            self.assertEqual(trust['Principal'], {'AWS':'arn:aws:iam::111111111111:role/Collector'})
            self.assertEqual(trust['Condition']['StringEquals']['sts:ExternalId'], self.source.external_id)
            self.assertEqual(bundle['minimum_permission_policy']['Statement'][1]['Resource'], self.source.role_arn)
            self.assertIn('organizations',bundle['optional_permission_policies'])
            with patch('billing.onboarding.boto3.client') as client:
                self.assertEqual(quick_create_url(self.source), f'/sources/{self.source.pk}/setup/')
                client.assert_not_called()

    def test_verification_checks_identity_and_capabilities(self):
        sts = Mock(); sts.get_caller_identity.return_value = {'Account': '123456789012'}
        orgs = Mock(); orgs.describe_organization.return_value = {'Organization': {'MasterAccountId': '123456789012'}}
        budgets = Mock(); budgets.describe_budgets.side_effect = ClientError({'Error': {'Code': 'AccessDeniedException'}}, 'DescribeBudgets')
        capabilities = verify_source(self.source, session=FakeSession(sts=sts, ce=Mock(), organizations=orgs, budgets=budgets), meter=Meter(limit=0))
        self.assertTrue(capabilities['organizations'])
        self.assertFalse(capabilities['budgets'])
        self.assertTrue(capabilities['cost_explorer'])
        sts.get_caller_identity.return_value = {'Account': '999999999999'}
        with self.assertRaises(ValueError):
            verify_source(self.source, session=FakeSession(sts=sts, ce=Mock()), meter=Meter(limit=0))

    def test_customer_status_reflects_connection_states(self):
        self.assertEqual(self.customer.status, 'Connected')
        BillingSource.objects.filter(pk=self.source.pk).update(last_success=timezone.now() - timedelta(hours=13))
        self.assertEqual(Customer.objects.get(pk=self.customer.pk).status, 'Stale data')
        BillingSource.objects.filter(pk=self.source.pk).update(last_error='AccessDenied: check trust')
        self.assertEqual(Customer.objects.get(pk=self.customer.pk).status, 'Permission problem')
        BillingSource.objects.filter(pk=self.source.pk).update(enabled=False)
        self.assertEqual(Customer.objects.get(pk=self.customer.pk).status, 'Paused')
        fresh, fresh_source = make_customer('Fresh', '333333333333', connected=False)
        self.assertEqual(fresh.status, 'Awaiting customer setup')


@web_settings
class ShellTests(TestCase):
    """The redesigned shell keeps authentication, navigation and CSRF-protected forms working."""

    def test_login_page_and_submission(self):
        User.objects.create_user('shell', password='test-only-a-long-password')
        client = Client(enforce_csrf_checks=True)
        page = client.get('/login/')
        self.assertContains(page, 'name="username"')
        self.assertContains(page, 'csrfmiddlewaretoken')
        token = client.cookies['csrftoken'].value
        response = client.post('/login/', {'username': 'shell', 'password': 'wrong', 'csrfmiddlewaretoken': token})
        self.assertContains(response, 'did not match')
        response = client.post('/login/', {'username': 'shell', 'password': 'test-only-a-long-password', 'csrfmiddlewaretoken': token})
        self.assertEqual(response.status_code, 302)
        home = client.get('/portfolio/')
        self.assertContains(home, 'data-nav-toggle')
        self.assertContains(home, 'aria-current="page"')
        self.assertContains(home, 'class="topbar"')
        self.assertEqual(client.get('/password/').status_code, 200)
