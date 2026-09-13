from datetime import date
from decimal import Decimal
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.test import TestCase, RequestFactory, override_settings
from django.utils import timezone
from billing import optimization
from billing.access import Access, context
from billing.models import Alert, Budget, BudgetAmount, CollectionPeriod, Cost, UserSecurity
from billing.scope import Scope
from billing.views_optimization import optimization as optimization_view
from .helpers import cost, make_customer, web_settings


class OptimizationTests(TestCase):
    def setUp(self):
        self.today = date(2026, 9, 10)
        self.month = self.today.replace(day=1)
        self.internal, self.source = make_customer('Flentas', '111111111111')
        self.external, self.other = make_customer('External Customer', '222222222222', accounts=('333333333333',))
        for day in range(1, 10):
            cost(self.source, date(2026, 9, day), '10')
        cost(self.other, self.month, '22')
        cost(self.other, self.month, '999', account_id='333333333333')
        self.budget = Budget.objects.create(customer=self.internal, scope=Budget.ACCOUNT,
            account_id=self.source.account_id, name='Internal account', currency='USD')
        BudgetAmount.objects.create(budget=self.budget, amount=Decimal('200'), effective_from=self.month)
        CollectionPeriod.objects.create(source=self.source, month=self.month, status='complete', first_day=self.month, last_day=date(2026, 9, 9), last_success=timezone.now())

    def report(self, **kwargs):
        return optimization.report(self.month, 'USD', 'unblended', Scope(), today=self.today, **kwargs)

    def test_complete_data_forecast_and_recorded_alarm_without_side_effects(self):
        Alert.objects.create(budget=self.budget, month=self.month, kind='forecast', threshold=100, message='Historical alarm')
        with patch('billing.budgets.aws_forecast', side_effect=AssertionError('No AWS call')):
            data = self.report()
        evaluation = data['budget_rows'][0]['evaluation']
        self.assertEqual(evaluation.actual, Decimal('90'))
        self.assertEqual(evaluation.forecast, Decimal('300'))
        self.assertEqual(evaluation.status, 'Forecast over budget')
        self.assertEqual(data['breach_count'], 1)
        self.assertEqual(len(data['alerts']), 1)
        self.assertEqual(Alert.objects.count(), 1)
        self.assertEqual(self.budget.evaluations.count(), 0)

    def test_partial_data_never_projects_or_marks_within_budget(self):
        CollectionPeriod.objects.update(status='partial')
        evaluation = self.report()['budget_rows'][0]['evaluation']
        self.assertIsNone(evaluation.forecast)
        self.assertEqual(evaluation.status, 'Unverified')

    def test_incomplete_dates_prevent_projection_even_after_successful_import(self):
        CollectionPeriod.objects.update(last_day=self.month)
        evaluation = self.report()['budget_rows'][0]['evaluation']
        self.assertIsNone(evaluation.forecast)
        self.assertEqual(evaluation.status, 'Unverified')

    def test_currency_and_metric_are_not_mixed(self):
        cost(self.source, self.today, '1000', currency='INR')
        self.assertEqual(self.report()['budget_rows'][0]['evaluation'].actual, Decimal('90'))
        Cost.objects.filter(source=self.source, currency='USD').update(amortized=5)
        data = optimization.report(self.month, 'USD', 'amortized', Scope(), today=self.today)
        self.assertEqual(data['budget_rows'], [])
        row = next(r for r in data['opportunities'] if r['customer_id'] == self.internal.pk)
        self.assertEqual(row['spend'], Decimal('45'))

    @override_settings(ENFORCE_CUSTOMER_AUTHORIZATION=True)
    def test_account_restricted_external_scope_excludes_other_accounts_and_internal(self):
        access = Access(1, 'external', False, (self.external.pk,), (), {self.external.pk: ['222222222222']})
        with context(access):
            data = self.report(show_budgets=False)
            self.assertFalse(data['show_budgets'])
            self.assertEqual(data['budget_rows'], [])
            self.assertEqual(list(data['alerts']), [])
            self.assertEqual(len(data['opportunities']), 1)
            self.assertEqual(data['opportunities'][0]['spend'], Decimal('22'))
            self.assertIsNone(data['opportunities'][0]['savings'])
            with self.assertRaises(ValueError):
                optimization.options({'customer': str(self.internal.pk)})

    def test_missing_budget_detection(self):
        self.budget.amounts.all().delete()
        self.assertEqual(self.report()['missing_budgets'][0]['account__account_id'], '111111111111')

    @web_settings
    def test_view_renders_and_external_profile_hides_flentas_budget_even_in_legacy_mode(self):
        user = get_user_model().objects.create_user('viewer', password='not-a-real-password')
        profile, _ = UserSecurity.objects.get_or_create(user=user)
        profile.external = True
        profile.save()
        request = RequestFactory().get('/optimization/', {'month': '2026-09'})
        request.user = user
        response = optimization_view(request)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Flentas budget control centre')
        self.assertContains(response, 'Savings review opportunities')
        self.assertContains(response, 'Estimated savings')
        self.assertContains(response, 'Unavailable')

    def test_invalid_options(self):
        for params in ({'month': 'bad'}, {'month': '9999-12'}, {'currency': 'USDX'}, {'metric': 'unsafe'}):
            with self.assertRaises(ValueError):
                optimization.options(params)

    def snapshot(self, **changes):
        from billing.models import ImportedBudget
        values = dict(source=self.source, owning_account_id=self.source.account_id, name='AWS account limit',
            budget_type='COST', time_unit='MONTHLY', limit_amount=Decimal('200'), limit_unit='USD',
            actual_amount=Decimal('100'), actual_unit='USD', forecast_amount=Decimal('280'), forecast_unit='USD',
            filters={'LinkedAccount': [self.source.account_id]}, imported_at=timezone.now().replace(year=2026, month=9))
        values.update(changes)
        return ImportedBudget.objects.create(**values)

    def test_owner_precedence_and_alarm_threshold_evidence(self):
        self.internal.assignments.update(metadata={'owner': 'Account engineer'})
        self.budget.forecast_threshold = 120
        self.budget.save()
        Alert.objects.create(budget=self.budget, month=self.month, kind='forecast', threshold=120, message='Review')
        data = self.report()
        self.assertEqual(data['budget_rows'][0]['owner'], 'Account engineer')
        self.assertTrue(data['budget_rows'][0]['forecast_threshold_reached'])
        self.assertEqual(data['budget_rows'][0]['forecast_overrun'], Decimal('100'))
        self.assertEqual(data['alerts'][0].responsible_owner, 'Account engineer')
        self.budget.owner = 'Budget owner'
        self.budget.save()
        self.assertEqual(self.report()['budget_rows'][0]['owner'], 'Budget owner')

    def test_missing_owner_is_explicit_and_imported_budget_counts_as_coverage(self):
        self.budget.amounts.all().delete()
        self.snapshot()
        data = self.report()
        self.assertEqual(data['missing_budgets'], [])
        row = data['imported_rows'][0]
        self.assertEqual(row['owner'], 'Unassigned')
        self.assertIn('/accounts/111111111111/', row['owner_url'])
        self.assertEqual(row['forecast_overrun'], Decimal('80'))
        self.assertEqual(row['actual_percent'], Decimal('50'))

    def test_filtered_payer_or_wrong_metric_imports_do_not_fill_account_limit(self):
        self.budget.amounts.all().delete()
        self.snapshot(filters={'LinkedAccount': [self.source.account_id], 'Service': ['Amazon EC2']})
        self.snapshot(name='Payer wide', filters={})
        self.snapshot(name='Amortized', raw={'CostTypes': {'UseAmortized': True}})
        data = self.report()
        self.assertEqual(len(data['missing_budgets']), 1)
        self.assertEqual(len(data['imported_rows']), 2)
        self.assertFalse(data['imported_rows'][0]['account_wide'])

    def test_imported_rows_respect_account_month_source_and_currency(self):
        self.snapshot()
        self.snapshot(name='INR', limit_unit='INR')
        self.snapshot(name='Old', imported_at=timezone.now().replace(year=2026, month=8))
        other = optimization.report(self.month, 'USD', 'unblended', Scope(account_id='333333333333'), today=self.today)
        self.assertEqual(other['imported_rows'], [])
        data = self.report()
        self.assertEqual(len(data['imported_rows']), 1)
        self.snapshot(name='Currency mismatch', forecast_unit='INR')
        row = next(r for r in self.report()['imported_rows'] if r['snapshot'].name == 'Currency mismatch')
        self.assertIsNone(row['forecast_overrun'])

    @web_settings
    def test_internal_view_shows_owner_thresholds_imported_overrun_and_history(self):
        self.snapshot()
        Alert.objects.create(budget=self.budget, month=self.month, kind='actual', threshold=80, message='Review actual')
        user = get_user_model().objects.create_superuser('budget-review', password='preview-only')
        request = RequestFactory().get('/optimization/', {'month': '2026-09'})
        request.user = user
        response = optimization_view(request)
        self.assertEqual(response.status_code, 200)
        for text in ('Responsible owner', 'Unassigned', 'Assign budget owner', 'Actual alarm: 80%', 'Forecast alarm: 100%', 'Over limit: 80.00', 'Notification thresholds unavailable'):
            self.assertContains(response, text)

    def test_imported_controls_prioritize_breaches_and_bound_rows(self):
        for number in range(25):
            self.snapshot(name=f'Budget {number:02}', forecast_amount=Decimal('10'))
        self.snapshot(name='Actual breach', actual_amount=Decimal('900'), forecast_amount=None)
        data = self.report()
        self.assertEqual(data['imported_total'], 26)
        self.assertEqual(len(data['imported_rows']), 20)
        self.assertEqual(data['imported_rows'][0]['snapshot'].name, 'Actual breach')
        self.assertEqual(data['imported_rows'][0]['actual_overrun'], Decimal('700'))
        self.assertEqual(data['missing_budgets'], [])

    def test_member_budget_connection_visible_in_account_scope(self):
        from billing.models import BillingSource
        member = BillingSource.objects.create(customer=self.internal, account_id=self.source.account_id,
            kind=BillingSource.MEMBER_BUDGETS, enabled=True)
        data = optimization.report(self.month, 'USD', 'unblended', Scope(account_id=self.source.account_id), today=self.today)
        self.assertIn(member.pk, [r['source'].pk for r in data['aws_budget_connections']])
