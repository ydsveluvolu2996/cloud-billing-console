from datetime import timedelta
from unittest.mock import patch
from django.test import TestCase, override_settings
from django.utils import timezone
from botocore.exceptions import ClientError
from billing.models import ImportedBudget, BillingSource, Job
from billing.aws_budget_display import account_snapshots
from billing import scheduler, jobs
from billing.access import Access, context
from .helpers import make_customer, cost, web_settings


@web_settings
class AWSBudgetDisplayTests(TestCase):
    def setUp(self):
        self.customer, self.source = make_customer('Flentas', '111111111111', accounts=['222222222222'])
        self.source.kind = BillingSource.PAYER; self.source.approved_capabilities = ['budgets']; self.source.save()
        self.month = timezone.now().date().replace(day=1)

    def snapshot(self, name='AWS member limit', **kwargs):
        defaults = dict(source=self.source, owning_account_id=self.source.account_id, name=name, budget_type='COST', time_unit='MONTHLY', limit_unit='USD', limit_amount=500, actual_amount=200, actual_unit='USD', filters={'LinkedAccount': ['222222222222']})
        defaults.update(kwargs)
        return ImportedBudget.objects.create(**defaults)

    def test_exact_member_filter_maps_and_payer_total_does_not(self):
        budget = self.snapshot()
        self.snapshot('Organization limit', filters={})
        result = account_snapshots([self.customer], self.month, 'USD')
        self.assertEqual(result[(self.customer.pk,'222222222222')], [budget])
        self.assertNotIn((self.customer.pk,'111111111111'), result)

    def test_multiple_accounts_currency_basis_and_old_snapshot_not_mapped(self):
        self.snapshot('Multiple', filters={'LinkedAccount': ['111111111111','222222222222']})
        self.snapshot('Currency', limit_unit='INR')
        self.snapshot('Amortized', raw={'CostTypes': {'UseAmortized': True}})
        self.snapshot('Historical', imported_at=timezone.now().replace(day=1)-timedelta(days=1))
        self.assertEqual(account_snapshots([self.customer], self.month, 'USD'), {})

    def test_member_reader_unfiltered_and_expired_budget(self):
        self.source.kind=BillingSource.MEMBER_BUDGETS; self.source.save()
        budget=self.snapshot(filters={})
        self.snapshot('Expired',time_period={'End':'2000-01-01T00:00:00Z'},filters={})
        self.assertEqual(account_snapshots([self.customer],self.month,'USD')[(self.customer.pk,'111111111111')],[budget])

    @override_settings(REQUIRE_CONNECTION_APPROVAL=True)
    def test_approved_but_previously_denied_schedules_automatic_retry(self):
        self.source.capabilities={'budgets':False};self.source.save()
        scheduler.schedule_due()
        self.assertTrue(Job.objects.filter(kind='import_budgets',source=self.source).exists())
        before=Job.objects.count();scheduler.schedule_due();self.assertEqual(Job.objects.count(),before)

    @override_settings(REQUIRE_CONNECTION_APPROVAL=True)
    def test_unapproved_budget_read_never_calls_aws(self):
        self.source.approved_capabilities=[];self.source.save()
        with patch('billing.scheduler.load_source',return_value=self.source), patch('billing.collector.import_budgets') as call:
            with self.assertRaises(jobs.PermanentJobError):scheduler.handle_import_budgets(Job(source=self.source))
            call.assert_not_called()

    def test_permission_failure_keeps_snapshot_and_recovers(self):
        self.snapshot()
        denied=ClientError({'Error':{'Code':'AccessDeniedException','Message':'Denied'}},'DescribeBudgets')
        with patch('billing.scheduler.load_source',return_value=self.source), patch('billing.collector.import_budgets',side_effect=denied):
            with self.assertRaises(jobs.PermanentJobError):scheduler.handle_import_budgets(Job(source=self.source))
        self.assertEqual(ImportedBudget.objects.count(),1)
        self.source.refresh_from_db();self.assertFalse(self.source.capabilities['budgets'])
        with patch('billing.scheduler.load_source',return_value=self.source), patch('billing.collector.import_budgets',return_value=1):
            self.assertEqual(scheduler.handle_import_budgets(Job(source=self.source)),{'budgets':1})
        self.source.refresh_from_db();self.assertTrue(self.source.capabilities['budgets']);self.assertNotIn('budgets_error',self.source.capabilities)

    @override_settings(ENFORCE_CUSTOMER_AUTHORIZATION=True)
    def test_unrelated_customer_snapshot_is_not_exposed(self):
        self.snapshot()
        other,_=make_customer('Other','333333333333')
        with context(Access(1,'reader',False,(other.pk,),(),{})):
            self.assertEqual(account_snapshots([self.customer],self.month,'USD'),{})

    def test_live_views_render_imported_account_budget_and_hide_external_column(self):
        from django.contrib.auth.models import User
        self.snapshot()
        cost(self.source, self.month, 20, account_id='222222222222')
        self.client.force_login(User.objects.create_user('budget-admin', is_staff=True))
        params = {'customer': str(self.customer.pk), 'start': str(self.month), 'end': str(self.month)}
        for path in ['/portfolio/', f'/customers/{self.customer.pk}/', '/optimization/']:
            response = self.client.get(path, params)
            self.assertContains(response, 'AWS member limit')
            self.assertContains(response, '500.00 USD')
        self.customer.name = 'External customer'
        self.customer.save()
        self.assertNotContains(self.client.get('/portfolio/', params), 'Configured budget')

    def test_member_budget_credit_refund_exclusion_maps_without_service_budget(self):
        self.source.kind = BillingSource.MEMBER_BUDGETS
        self.source.save()
        budget = self.snapshot(filters={'Not': {'Dimensions': {'Key': 'RECORD_TYPE', 'Values': ['Credit', 'Refund']}}})
        self.snapshot('EC2-only', filters={'Dimensions': {'Key': 'SERVICE', 'Values': ['Amazon EC2']}})
        self.assertEqual(account_snapshots([self.customer], self.month, 'USD')[(self.customer.pk, self.source.account_id)], [budget])

    def test_budget_reader_verification_does_not_collect_inventory_or_costs(self):
        self.source.kind = BillingSource.MEMBER_BUDGETS
        self.source.save()
        with patch('billing.scheduler.load_source', return_value=self.source), patch('billing.collector.verify_source', return_value={'budgets': True}), patch('billing.collector.discover_accounts') as discovery:
            scheduler.handle_verify(Job(source=self.source))
            self.assertEqual(scheduler.handle_discover(Job(source=self.source)), {'accounts': 0, 'mode': 'member_budgets'})
            discovery.assert_not_called()
        self.assertTrue(Job.objects.filter(source=self.source, kind='import_budgets').exists())
        self.assertFalse(Job.objects.filter(source=self.source, kind='discover').exists())
