from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import Mock, patch
import yaml
from botocore.exceptions import ClientError
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings, Client
from django.utils import timezone
from billing.collector import sync_customer, verify
from billing.models import Cost, Customer
from billing.onboarding import customer_template
from billing.reporting import report


@override_settings(SECURE_SSL_REDIRECT=False, STORAGES={'default': {'BACKEND':'django.core.files.storage.FileSystemStorage'}, 'staticfiles': {'BACKEND':'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class BillingTests(TestCase):
    def setUp(self):
        self.customer = Customer.objects.create(name='Test customer', account_id='123456789012',
            role_arn='arn:aws:iam::123456789012:role/BillingConsole/CostReadOnly', last_success=timezone.now())
        self.admin = User.objects.create_user('owner', password='test-only-a-long-password', is_staff=True)
        self.reader = User.objects.create_user('reader', password='test-only-a-long-password')

    def response(self, amount='10.10', day='2026-09-01', token=None):
        r = {'ResultsByTime': [{'TimePeriod': {'Start':day, 'End':str(date.fromisoformat(day)+timedelta(days=1))},
          'Estimated':True, 'Groups':[{'Keys':['123456789012','Amazon EC2'], 'Metrics': {
          'UnblendedCost': {'Amount':amount, 'Unit':'USD'}, 'AmortizedCost': {'Amount':amount,'Unit':'USD'}}}]}]}
        if token:
            r['NextPageToken'] = token
        return r

    def existing(self, day, currency='USD', amount='90'):
        return Cost.objects.create(customer=self.customer, day=day, account_id='123456789012',
            service='Amazon EC2', currency=currency, unblended=Decimal(amount), amortized=Decimal(amount))

    def test_sync_replaces_revisions_without_duplicates(self):
        ce = Mock()
        ce.get_cost_and_usage.side_effect = [{}, self.response(), {}, self.response('11.25')]
        for _ in range(2):
            self.assertEqual(sync_customer(self.customer, ce, today=date(2026,9,8)).status, 'success')
        self.assertEqual(Cost.objects.count(), 1)
        self.assertEqual(Cost.objects.get().unblended, Decimal('11.25'))

    def test_failure_on_later_page_preserves_all_previous_data(self):
        old = self.existing(date(2026,9,1))
        before = self.customer.last_success
        ce = Mock()
        ce.get_cost_and_usage.side_effect = [{}, self.response(token='next'), ClientError({'Error':{'Code':'AccessDenied'}}, 'GetCostAndUsage')]
        run = sync_customer(self.customer, ce, today=date(2026,9,8))
        self.assertEqual(run.status, 'failed')
        self.assertEqual(Cost.objects.get(pk=old.pk).unblended, 90)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.last_success, before)

    def test_pagination_and_negative_credits(self):
        ce = Mock()
        ce.get_cost_and_usage.side_effect = [{}, self.response(token='next'), self.response('-2.50', day='2026-09-02')]
        run = sync_customer(self.customer, ce, today=date(2026,9,8))
        self.assertEqual(run.status, 'success')
        self.assertEqual(Cost.objects.count(), 2)
        self.assertEqual(ce.get_cost_and_usage.call_args.kwargs['NextPageToken'], 'next')
        self.assertEqual(sum(Cost.objects.values_list('unblended',flat=True)), Decimal('7.60'))

    def test_paused_customer_never_calls_aws(self):
        self.customer.enabled=False
        ce=Mock()
        self.assertIsNone(sync_customer(self.customer, ce))
        ce.get_cost_and_usage.assert_not_called()

    def test_role_must_match_customer_account_and_restricted_path(self):
        self.customer.role_arn = 'arn:aws:iam::999999999999:role/Admin'
        with self.assertRaises(ValidationError): self.customer.full_clean()

    def test_overlapping_account_import_is_rejected(self):
        other=Customer.objects.create(name='Existing payer',account_id='222222222222')
        old=self.existing(date(2026,9,1))
        old.customer=other
        old.save()
        ce=Mock()
        ce.get_cost_and_usage.side_effect=[{},self.response()]
        run=sync_customer(self.customer,ce,today=date(2026,9,8))
        self.assertEqual(run.status,'failed')
        self.assertIn('already assigned',run.error)
        self.assertEqual(Cost.objects.count(),1)

    def test_budget_uses_whole_customer_despite_service_filter(self):
        today=timezone.now().date()
        self.existing(today,amount='10')
        Cost.objects.create(customer=self.customer,day=today,account_id='123456789012',service='Amazon S3',
            currency='USD',unblended=Decimal('20'),amortized=Decimal('20'))
        self.customer.budget=Decimal('25')
        self.customer.save()
        result=report({'service':'Amazon EC2'})
        self.assertEqual(result['total'],10)
        self.assertEqual(result['rows'][0]['mtd'],30)
        self.assertTrue(result['rows'][0]['over_budget'])

    def test_mixed_currencies_are_not_summed(self):
        today=timezone.now().date()
        self.existing(today, 'USD', '10')
        self.existing(today, 'INR', '800')
        self.assertEqual(report({'currency':'USD'})['total'], 10)
        self.assertEqual(report({'currency':'INR'})['total'], 800)

    def test_date_range_validation(self):
        with self.assertRaises(ValueError): report({'start':'2026-04-20','end':'2026-04-01'})
        with self.assertRaises(ValueError): report({'start':'not-a-date'})

    def test_anonymous_access_redirects_and_csrf_is_enforced(self):
        for path in ['/', '/customers/', '/activity/', '/export/', f'/customers/{self.customer.pk}/template/']:
            self.assertEqual(self.client.get(path).status_code,302)
        secure_client=Client(enforce_csrf_checks=True)
        secure_client.force_login(self.admin)
        self.assertEqual(secure_client.post('/customers/add/', {'name':'Bad','account_id':'234567890123'}).status_code,403)

    def test_reader_cannot_modify_customer(self):
        self.client.force_login(self.reader)
        self.assertEqual(self.client.get('/').status_code, 200)
        self.assertEqual(self.client.post('/customers/add/',{}).status_code,403)
        self.assertEqual(self.client.post(f'/customers/{self.customer.pk}/sync/').status_code,403)

    def test_all_main_pages_render(self):
        self.client.force_login(self.admin)
        for path in ['/', '/customers/', '/customers/add/', '/activity/', f'/customers/{self.customer.pk}/']:
            response=self.client.get(path)
            self.assertEqual(response.status_code,200, path)

    def test_csv_escapes_formula_injection_and_respects_currency(self):
        self.customer.name='=HYPERLINK("https://example.com")'
        self.customer.save()
        self.existing(timezone.now().date(), 'USD','12.5')
        self.existing(timezone.now().date(), 'INR','1000')
        self.client.force_login(self.admin)
        data=self.client.get('/export/?currency=USD').content.decode()
        self.assertIn("'=HYPERLINK",data)
        self.assertNotIn('1000.0000000000',data)

    @override_settings(COLLECTOR_ROLE_ARN='arn:aws:iam::111111111111:role/CloudBillingCollector')
    def test_onboarding_template_scopes_trust_and_only_cost_read(self):
        template=yaml.safe_load(customer_template(self.customer))
        role=template['Resources']['CostReadRole']['Properties']
        self.assertEqual(template['Parameters']['ExternalId']['Default'],str(self.customer.external_id))
        self.assertEqual(role['Policies'][0]['PolicyDocument']['Statement'][0]['Action'],'ce:GetCostAndUsage')
        self.assertIn('sts:ExternalId', role['AssumeRolePolicyDocument']['Statement'][0]['Condition']['StringEquals'])

    @patch('billing.collector.cost_client')
    def test_verified_connection_queues_initial_import(self, make_client):
        make_client.return_value.get_cost_and_usage.return_value={}
        self.client.force_login(self.admin)
        self.client.post(f'/customers/{self.customer.pk}/', {'action':'connection','role_arn':self.customer.role_arn})
        self.customer.refresh_from_db()
        self.assertTrue(self.customer.sync_requested)
        self.assertIsNotNone(self.customer.verified_at)
