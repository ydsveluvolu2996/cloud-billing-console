from decimal import Decimal
from types import SimpleNamespace
from django.test import SimpleTestCase
from django.template.loader import render_to_string
from billing.templatetags.budget_usage import account_usage, usage_item


class CompactBudgetTests(SimpleTestCase):
    def item(self, limit=100, actual=25, **kw):
        return usage_item('Monthly', Decimal(limit) if limit is not None else None,
                          Decimal(actual) if actual is not None else None, 'USD', '/budgets/', **kw)

    def test_status_bar_and_zero_limit(self):
        self.assertEqual(self.item()['status'], 'Within budget')
        self.assertEqual(self.item()['fill'], 25)
        self.assertEqual(self.item(actual=80)['status'], 'Near limit')
        self.assertEqual(self.item(actual=100)['status'], 'At limit')
        breached = self.item(actual=125)
        self.assertEqual((breached['status'], breached['fill'], breached['percent']), ('Breached', 100, 125))
        self.assertEqual(self.item(limit=0, actual=1)['status'], 'Breached')
        self.assertEqual(self.item(limit=0, actual=0)['status'], 'At limit')
        self.assertEqual(self.item(actual=-10)['fill'], 0)

    def test_missing_stale_unverified_are_not_healthy(self):
        self.assertEqual(self.item(actual=None)['status'], 'Usage unavailable')
        self.assertEqual(self.item(stale=True)['status'], 'Data stale')
        self.assertEqual(self.item(verified=False)['status'], 'Unverified spend')
        self.assertEqual(self.item(limit=None)['status'], 'Not set')

    def snapshot(self, name, amount, actual=25, unit='USD'):
        return SimpleNamespace(name=name, limit_amount=Decimal(amount), actual_amount=Decimal(actual),
                               actual_unit=unit, limit_unit='USD', filters={}, snapshot_stale=False)

    def test_multiple_budgets_are_separate_and_highest_usage_is_primary(self):
        row = {'aws_budgets': [self.snapshot('Main', 100), self.snapshot('Small', 10)]}
        result = account_usage(row)
        self.assertEqual(result['count'], 2)
        self.assertEqual(result['primary']['limit'], 10)
        self.assertEqual(result['primary']['status'], 'Breached')
        html = render_to_string('billing/includes/account_budget_cell.html', {'row': row, 'currency': 'USD'})
        self.assertIn('$25.00', html)
        self.assertIn('of $10.00', html)
        self.assertIn('2 budgets · highest usage', html)
        self.assertNotIn('<details open', html)
        self.assertNotIn('Forecast', html)
        self.assertNotIn('<td', html)
        self.assertEqual(html.split('<details')[0].count('<progress'), 1)

    def test_currency_mismatch_and_filtered_local_budget_do_not_use_account_spend(self):
        row = {'aws_budgets': [self.snapshot('Different unit', 100, unit='INR')]}
        self.assertIsNone(account_usage(row)['primary']['actual'])
        budget = SimpleNamespace(pk=1, name='EC2 only', filters={'service': ['EC2']})
        row = {'mtd': Decimal(90), 'configured_budgets': [{'budget':budget, 'amount':Decimal(100)}]}
        self.assertIsNone(account_usage(row)['primary']['actual'])

    def test_no_budget_is_short_and_has_no_false_bar(self):
        html = render_to_string('billing/includes/account_budget_cell.html', {'row': {}, 'currency': 'USD'})
        self.assertIn('No account budget', html)
        self.assertNotIn('<progress', html)
