from django.test import override_settings
"""Budget evaluation: history, overrides, forecasts, data status, alerts, portfolio totals, CSV."""
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import Mock
from django.test import TestCase
from django.utils import timezone
from billing import budgets
from billing.models import Alert, Budget, BudgetAmount, BudgetEvaluation, CollectionPeriod, ExplorerQuery, Project, AllocationRule
from billing.query_cache import run_query
from .helpers import cost, make_customer


@override_settings(REQUIRE_CONNECTION_APPROVAL=False)
class BudgetTests(TestCase):
    def setUp(self):
        self.customer, self.source = make_customer('Budget Co', '111111111111', accounts=('222222222222',))
        self.today = date(2026, 9, 10)
        self.now = timezone.now().replace(year=2026, month=9, day=10, hour=12)
        self.month = date(2026, 9, 1)
        for day in range(1, 10):  # nine completed days of 10 USD, today 5 USD
            cost(self.source, date(2026, 9, day), '10')
        cost(self.source, self.today, '5')
        CollectionPeriod.objects.create(source=self.source, month=self.month, status='complete', last_success=self.now)
        self.budget = Budget.objects.create(customer=self.customer, scope=Budget.CUSTOMER, name='Monthly', currency='USD')
        BudgetAmount.objects.create(budget=self.budget, amount=Decimal('200'), effective_from=date(2026, 1, 1))

    def evaluate(self, budget=None, month=None, persist=True):
        return budgets.evaluate_budget(budget or self.budget, month or self.month, today=self.today, persist=persist, now=self.now)

    def test_run_rate_forecast_and_status(self):
        e = self.evaluate()
        self.assertEqual(e.actual, Decimal('95'))
        self.assertEqual(e.forecast_method, 'run_rate')
        self.assertEqual(e.forecast, Decimal('90') / 9 * 30)  # 300
        self.assertEqual(e.status, 'Forecast over budget')
        self.assertEqual(e.remaining, Decimal('105'))
        self.assertAlmostEqual(e.percent, 47.5)
        self.assertEqual(e.forecast_variance, Decimal('100'))
        alerts = Alert.objects.filter(budget=self.budget)
        self.assertEqual(list(alerts.values_list('kind', flat=True)), ['forecast'])
        self.evaluate()
        self.assertEqual(Alert.objects.count(), 1)  # deduplicated

    def test_overspend_actual_alert_and_acknowledgement(self):
        BudgetAmount.objects.create(budget=self.budget, amount=Decimal('50'), month=self.month)  # override for this month
        e = self.evaluate()
        self.assertEqual(e.amount, Decimal('50'))
        self.assertEqual(e.status, 'Over budget')
        self.assertLess(e.remaining, 0)
        actual_alert = Alert.objects.get(kind='actual')
        self.assertEqual(actual_alert.threshold, Decimal('80'))
        actual_alert.acknowledged_by = 'ops'
        actual_alert.save()
        self.evaluate()
        self.assertEqual(Alert.objects.filter(kind='actual').count(), 1)

    def test_budget_history_uses_amount_effective_for_month(self):
        budgets.set_recurring_amount(self.budget, Decimal('400'), date(2026, 10, 1))
        self.assertEqual(self.budget.amount_for(date(2026, 9, 1)), Decimal('200'))
        self.assertEqual(self.budget.amount_for(date(2026, 10, 1)), Decimal('400'))
        self.assertEqual(self.budget.amount_for(date(2025, 12, 1)), None)

    def test_missing_budget_and_missing_data(self):
        empty = Budget.objects.create(customer=self.customer, scope=Budget.CUSTOMER, name='Empty')
        self.assertEqual(self.evaluate(empty).status, 'Not configured')
        e = self.evaluate(month=date(2026, 8, 1))
        self.assertEqual(e.status, 'No data')
        self.assertEqual(e.data_status, 'missing')
        self.assertIsNone(e.actual)

    def test_stale_or_partial_data_never_reports_within_budget(self):
        BudgetAmount.objects.create(budget=self.budget, amount=Decimal('100000'), month=self.month)
        CollectionPeriod.objects.filter(source=self.source).update(status='partial')
        e = self.evaluate()
        self.assertEqual(e.status, 'Unverified')
        self.assertEqual(e.data_status, 'partial')
        CollectionPeriod.objects.filter(source=self.source).update(status='complete', last_success=self.now - timedelta(hours=20))
        e = self.evaluate()
        self.assertEqual((e.status, e.data_status), ('Unverified', 'stale'))
        CollectionPeriod.objects.filter(source=self.source).update(last_success=self.now)
        self.assertEqual(self.evaluate().status, 'Within budget')

    def test_aws_forecast_preferred_when_available(self):
        self.evaluate()  # queues the forecast query
        query = ExplorerQuery.objects.get(operation='get_cost_forecast')
        self.assertEqual(query.parameters['TimePeriod'], {'Start': '2026-09-10', 'End': '2026-10-01'})
        self.assertEqual(query.parameters['Metric'], 'UNBLENDED_COST')
        client = Mock()
        client.get_cost_forecast.return_value = {'ForecastResultsByTime': [{'TimePeriod': {'Start': '2026-09-10', 'End': '2026-10-01'}, 'MeanValue': '60'}]}
        run_query(query, client)
        e = self.evaluate()
        self.assertEqual(e.forecast_method, 'aws')
        self.assertEqual(e.forecast, Decimal('90') + Decimal('60'))

    def test_run_rate_unavailable_with_insufficient_history(self):
        e = budgets.evaluate_budget(self.budget, self.month, today=date(2026, 9, 2), now=self.now.replace(day=2), persist=False)
        self.assertEqual(e.forecast_method, 'unavailable')
        self.assertIsNone(e.forecast)

    def test_account_and_payer_scopes_and_service_filters(self):
        cost(self.source, date(2026, 9, 3), '40', account_id='222222222222', service='Amazon S3')
        account_budget = Budget.objects.create(customer=self.customer, scope=Budget.ACCOUNT, account_id='222222222222', name='Prod')
        BudgetAmount.objects.create(budget=account_budget, amount=Decimal('30'))
        self.assertEqual(self.evaluate(account_budget).actual, Decimal('40'))
        payer_budget = Budget.objects.create(customer=self.customer, scope=Budget.SOURCE, source=self.source, name='Payer', filters={'exclude_services': ['Amazon S3']})
        BudgetAmount.objects.create(budget=payer_budget, amount=Decimal('300'))
        self.assertEqual(self.evaluate(payer_budget).actual, Decimal('95'))
        eur = Budget.objects.create(customer=self.customer, scope=Budget.CUSTOMER, name='EUR', currency='EUR')
        BudgetAmount.objects.create(budget=eur, amount=Decimal('10'))
        self.assertIsNone(self.evaluate(eur).actual)  # currencies never mix

    def test_project_budget_uses_allocated_costs(self):
        project = Project.objects.create(customer=self.customer, name='ERP')
        AllocationRule.objects.create(project=project, kind='accounts', account_ids=['222222222222'])
        cost(self.source, date(2026, 9, 3), '40', account_id='222222222222')
        from billing import allocation
        allocation.allocate_project(project, today=self.today)
        budget = Budget.objects.create(customer=self.customer, scope=Budget.PROJECT, project=project, name='ERP budget')
        BudgetAmount.objects.create(budget=budget, amount=Decimal('100'))
        self.assertEqual(self.evaluate(budget).actual, Decimal('40'))

    def test_portfolio_summary_never_adds_child_budgets_to_parent(self):
        child = Budget.objects.create(customer=self.customer, scope=Budget.ACCOUNT, account_id='222222222222', name='Prod')
        BudgetAmount.objects.create(budget=child, amount=Decimal('150'))
        self.evaluate(); self.evaluate(child)
        summary = budgets.portfolio_summary(self.month)
        self.assertEqual(summary['customer_budget_total'], {'USD': Decimal('200')})
        self.assertEqual(summary['customer_budgets'], 1)
        self.assertEqual(summary['child_budgets'], 1)
        check = budgets.child_allocation_check(self.customer, self.month)
        self.assertEqual(check['children'][0]['total'], Decimal('150'))
        self.assertFalse(check['children'][0]['exceeds_parent'])

    def test_bulk_csv_preview_and_apply(self):
        text = ('customer,scope,name,amount,currency,metric,payer_account,account_id,project,effective_from\n'
                'Budget Co,customer,Monthly,250,USD,unblended,,,,2026-09-01\n'
                'Budget Co,account,Prod account,90,USD,amortized,,222222222222,,2026-09-01\n'
                'Nobody,customer,X,10,USD,,,,,\n'
                'Budget Co,account,Bad,10,USD,,,999999999999,,\n')
        rows, errors = budgets.parse_budget_csv(text)
        self.assertEqual(len(rows), 4)
        self.assertEqual(len(errors), 2)
        self.assertEqual(rows[0]['action'], 'update')
        self.assertEqual(rows[1]['action'], 'create')
        with self.assertRaises(ValueError):
            budgets.apply_budget_rows(rows, 'ops')
        created, updated = budgets.apply_budget_rows(rows[:2], 'ops')
        self.assertEqual((created, updated), (1, 1))
        self.assertEqual(self.budget.amount_for(date(2026, 9, 1)), Decimal('250'))
        self.assertEqual(self.budget.amount_for(date(2026, 8, 1)), Decimal('200'))
        self.assertEqual(Budget.objects.get(name='Prod account').metric, 'amortized')

    def test_evaluate_all_persists_and_skips_offboarded(self):
        other, _ = make_customer('Gone', '999999999999')
        other.active = False
        other.save()
        Budget.objects.create(customer=other, scope=Budget.CUSTOMER, name='x')
        count = budgets.evaluate_all(today=self.today)
        self.assertEqual(count, 2)  # current and previous month for the active budget
        self.assertEqual(BudgetEvaluation.objects.filter(budget=self.budget).count(), 2)
