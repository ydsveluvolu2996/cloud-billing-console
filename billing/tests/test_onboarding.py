"""Onboarding wizard, bulk CSV, duplicate detection, rotation, pause and offboarding."""
from datetime import date
from decimal import Decimal
from unittest.mock import Mock, patch
from botocore.exceptions import ClientError
from django.contrib.auth.models import User
from django.test import TestCase, Client
from django.utils import timezone
from billing import jobs, onboarding, scheduler
from billing.models import AccountAssignment, AuditEvent, BillingSource, Budget, Customer, ExplorerQuery, Job
from .helpers import FakeSession, assign, cost, make_customer, web_settings


@web_settings
class OnboardingTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user('ops', password='test-only-a-long-password', is_staff=True)
        self.client = Client()
        self.client.force_login(self.admin)

    def test_wizard_creates_customer_connection_and_queues_verification(self):
        response = self.client.post('/customers/add/', {'name': 'Wizard Co', 'reference': 'CRM-1', 'owner': 'Sam', 'currency': 'USD', 'budget': '1200'})
        customer = Customer.objects.get(name='Wizard Co')
        self.assertRedirects(response, f'/customers/{customer.pk}/sources/add/')
        self.assertEqual(Budget.objects.get(customer=customer).amount_for(timezone.now().date().replace(day=1)), Decimal('1200'))
        response = self.client.post(f'/customers/{customer.pk}/sources/add/', {'kind': 'payer', 'account_id': '123456789012'})
        source = BillingSource.objects.get(customer=customer)
        self.assertRedirects(response, f'/sources/{source.pk}/')
        self.assertEqual(source.state, 'Awaiting customer setup')
        self.assertEqual(len(source.external_id), 40)
        page = self.client.get(f'/sources/{source.pk}/')
        self.assertContains(page, 'Generate setup link')
        response = self.client.post(f'/sources/{source.pk}/', {'action': 'connection', 'role_arn': 'arn:aws:iam::123456789012:role/BillingConsole/CostReadOnly'})
        self.assertEqual(response.status_code, 302)
        job = Job.objects.get(kind='verify', source=source)
        self.assertEqual(job.status, Job.QUEUED)
        # wrong account in the ARN is rejected by validation
        response = self.client.post(f'/sources/{source.pk}/', {'action': 'connection', 'role_arn': 'arn:aws:iam::999999999999:role/BillingConsole/CostReadOnly'})
        self.assertContains(response, 'onboarding template')

    def test_verification_job_records_states_then_discovery_then_import(self):
        customer, source = make_customer('Flow', '123456789012', connected=False)
        source.role_arn = source.expected_role_arn
        source.save()
        scheduler.request_verification(source)
        sts = Mock(); sts.get_caller_identity.return_value = {'Account': '123456789012'}
        orgs = Mock(); orgs.describe_organization.return_value = {'Organization': {'MasterAccountId': '123456789012'}}
        orgs.list_accounts.return_value = {'Accounts': [{'Id': '123456789012', 'Name': 'Mgmt', 'State': 'ACTIVE'}, {'Id': '210987654321', 'Name': 'Prod', 'State': 'ACTIVE'}]}
        budgets_client = Mock(); budgets_client.describe_budgets.return_value = {'Budgets': []}
        with patch('billing.collector.Session', return_value=FakeSession(sts=sts, ce=Mock(), organizations=orgs, budgets=budgets_client)):
            jobs.run_once(kinds=['verify'])
            source.refresh_from_db()
            self.assertEqual(source.state, 'Connection verified')
            self.assertTrue(source.capabilities['organizations'])
            self.assertTrue(Job.objects.filter(kind='discover', source=source, status=Job.QUEUED).exists())
            jobs.run_once(kinds=['discover'])
        source.refresh_from_db()
        self.assertEqual(source.state, 'Account discovery complete')
        self.assertEqual(AccountAssignment.objects.filter(customer=customer).count(), 2)
        self.client.post(f'/sources/{source.pk}/import/')
        source.refresh_from_db()
        self.assertEqual(source.onboarding_step, 6)
        job = Job.objects.get(kind='collect', source=source)
        self.assertEqual(job.payload['months_back'], 6)
        BillingSource.objects.filter(pk=source.pk).update(last_attempt=timezone.now())
        self.assertEqual(BillingSource.objects.get(pk=source.pk).state, 'Initial import running')

    def test_failed_verification_shows_permission_problem(self):
        customer, source = make_customer('Denied', '123456789012', connected=False)
        source.role_arn = source.expected_role_arn
        source.save()
        scheduler.request_verification(source)
        sts = Mock(); sts.get_caller_identity.side_effect = ClientError({'Error': {'Code': 'AccessDenied', 'Message': 'secret detail'}}, 'AssumeRole')
        with patch('billing.collector.Session', return_value=FakeSession(sts=sts)):
            jobs.run_once()
        source.refresh_from_db()
        self.assertIn('AccessDenied', source.last_error)
        self.assertNotIn('secret detail', source.last_error)
        self.assertEqual(Job.objects.get(kind='verify').status, Job.FAILED)
        self.assertIsNone(source.verified_at)

    def test_duplicate_payer_and_member_onboarding_detected(self):
        customer, source = make_customer('Existing', '123456789012', accounts=('210987654321',))
        other = Customer.objects.create(name='Other')
        response = self.client.post(f'/customers/{other.pk}/sources/add/', {'kind': 'payer', 'account_id': '123456789012'})
        self.assertContains(response, 'already connected')
        response = self.client.post(f'/customers/{other.pk}/sources/add/', {'kind': 'standalone', 'account_id': '210987654321'})
        self.assertContains(response, 'member of organization payer')
        self.assertEqual(BillingSource.objects.count(), 1)
        self.assertTrue(onboarding.detect_conflicts('210987654321'))

    def test_bulk_csv_preview_validation_and_apply(self):
        make_customer('Existing', '123456789012')
        text = ('name,reference,owner,currency,account_id,kind,shared,budget\n'
                'Alpha,CRM-1,Ann,USD,111111111111,payer,false,500\n'
                'Beta,CRM-2,Bob,EUR,222222222222,standalone,,\n'
                'Gamma,,,USD,123456789012,payer,,\n'
                'Alpha,,,USD,333333333333,payer,,\n'
                'Delta,,,USD,12345,payer,,\n')
        rows, errors = onboarding.parse_customer_csv(text)
        self.assertEqual(len(rows), 5)
        self.assertTrue(any('already connected' in e for e in errors))
        self.assertTrue(any('duplicate name' in e for e in errors))
        self.assertTrue(any('12 digits' in e for e in errors))
        good, errors = onboarding.parse_customer_csv(text.split('Gamma')[0])
        self.assertEqual(errors, [])
        created = onboarding.apply_customer_rows(good, 'ops')
        self.assertEqual(len(created), 2)
        self.assertEqual(Customer.objects.get(name='Alpha').budgets.count(), 1)
        self.assertEqual(BillingSource.objects.get(account_id='222222222222').kind, 'standalone')
        # HTTP flow: upload → preview → apply
        from django.core.files.uploadedfile import SimpleUploadedFile
        response = self.client.post('/onboarding/bulk/', {'file': SimpleUploadedFile('c.csv', b'name,account_id\nEpsilon,444444444444\n')})
        self.assertContains(response, 'All rows valid')
        import re
        import_id = re.search(r'name="import_id" value="(\d+)"', response.content.decode()).group(1)
        response = self.client.post('/onboarding/bulk/', {'action': 'apply', 'import_id': import_id})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(BillingSource.objects.filter(account_id='444444444444').exists())

    def test_rotation_invalidates_cache_and_requires_reverification(self):
        customer, source = make_customer('Rotate', '123456789012')
        ExplorerQuery.objects.create(source=source, fingerprint='f', operation='get_cost_and_usage', connection_fingerprint='old', requested=True)
        old_id = source.external_id
        rotated = onboarding.rotate_external_id(source, actor='ops')
        self.assertNotEqual(rotated.external_id, old_id)
        self.assertEqual(rotated.connection_version, 2)
        self.assertIsNone(rotated.verified_at)
        self.assertEqual(rotated.state, 'Awaiting customer setup')
        self.assertFalse(ExplorerQuery.objects.get().requested)
        self.assertTrue(AuditEvent.objects.filter(action='External ID rotated', source=rotated).exists())

    def test_pause_resume_and_offboarding_retain_history(self):
        customer, source = make_customer('Leaving', '123456789012', accounts=('210987654321',))
        cost(source, date(2026, 8, 1), '12')
        scheduler.request_refresh(source)
        onboarding.set_paused(source, True, actor='ops')
        self.assertFalse(BillingSource.objects.get(pk=source.pk).enabled)
        self.assertEqual(Job.objects.filter(source=source, status=Job.QUEUED).count(), 0)
        onboarding.set_paused(source, False, actor='ops')
        self.assertTrue(BillingSource.objects.get(pk=source.pk).enabled)
        response = self.client.post(f'/customers/{customer.pk}/offboard/')
        self.assertEqual(response.status_code, 302)
        customer.refresh_from_db()
        self.assertFalse(customer.active)
        self.assertEqual(customer.status, 'Offboarded')
        self.assertEqual(customer.costs.count(), 1)
        self.assertFalse(AccountAssignment.objects.filter(customer=customer, end__isnull=True).exists())
        self.assertFalse(scheduler.active_sources().filter(pk=source.pk).exists())
        # offboarded customers stay out of the default directory but keep their detail page
        self.assertNotContains(self.client.get('/customers/'), 'Leaving')
        self.assertContains(self.client.get('/customers/?show=offboarded'), 'Leaving')
        self.assertEqual(self.client.get(f'/customers/{customer.pk}/').status_code, 200)

    def test_onboarding_operations_view_filters_states(self):
        make_customer('Ready', '123456789012')
        make_customer('Pending', '222222222222', connected=False)
        response = self.client.get('/onboarding/?state=Awaiting+customer+setup')
        self.assertContains(response, 'Pending')
        self.assertNotContains(response, '>Ready<')
        response = self.client.get('/onboarding/?q=1234&sort=state&dir=desc')
        self.assertContains(response, 'Ready')

    def test_customer_directory_search_sort_pagination(self):
        for i in range(30):
            make_customer(f'Customer {i:02d}', f'{100000000000 + i:012d}')
        response = self.client.get('/customers/?sort=name&dir=desc')
        self.assertContains(response, 'Page 1 of 2')
        self.assertContains(response, 'Customer 29')
        response = self.client.get('/customers/?q=100000000007')
        self.assertContains(response, 'Customer 07')
        self.assertNotContains(response, 'Customer 08')
        response = self.client.get('/customers/?page=2')
        self.assertContains(response, 'Page 2 of 2')
