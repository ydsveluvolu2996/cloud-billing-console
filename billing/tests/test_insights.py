from datetime import date
from decimal import Decimal
from django.contrib.auth.models import User
from django.test import TestCase, RequestFactory, override_settings
from django.utils import timezone
from billing.access import Access, context
from billing.insights import executive_insights
from billing.models import CollectionPeriod
from billing.views_insights import insights
from .helpers import cost, make_customer, web_settings


@web_settings
class ExecutiveInsightsTests(TestCase):
    def setUp(self):
        self.customer, self.source = make_customer('Flentas', '123456789012')
        self.today = date(2026, 9, 12)
        for month, amount in [(date(2026, 8, 1), '100'), (date(2026, 9, 1), '150')]:
            cost(self.source, month, amount)
            CollectionPeriod.objects.create(source=self.source, month=month, status='complete', first_day=month,
                last_day=month.replace(day=11), last_success=timezone.now())

    def test_comparable_totals_forecast_and_explanations(self):
        result = executive_insights({}, self.today)
        self.assertTrue(result['comparable'])
        self.assertEqual(result['delta'], Decimal('50'))
        self.assertEqual(result['forecast'], Decimal(150) / 11 * 30)
        self.assertEqual(result['dimensions'][2]['rows'][0]['direction'], 'Increase')
        self.assertEqual(result['dimensions'][2]['rows'][0]['percent'], Decimal('50'))

    def test_partial_source_does_not_become_savings(self):
        CollectionPeriod.objects.filter(month=date(2026, 9, 1)).update(status='partial')
        result = executive_insights({}, self.today)
        self.assertFalse(result['comparable'])
        self.assertIsNone(result['delta'])
        self.assertIsNone(result['forecast'])
        self.assertIsNone(result['dimensions'][0]['rows'][0]['delta'])

    def test_new_customer_without_import_blocks_portfolio_comparison(self):
        make_customer('New customer', '987654321012', connected=False)
        result = executive_insights({}, self.today)
        self.assertFalse(result['comparable'])
        self.assertEqual(len(result['freshness']), 2)
        selected = executive_insights({'customer':str(self.customer.pk)}, self.today)
        self.assertTrue(selected['comparable'])

    def test_missing_service_zero_only_with_complete_currency_coverage(self):
        cost(self.source, date(2026, 9, 1), 20, service='S3')
        result = executive_insights({}, self.today)
        row = next(r for r in result['dimensions'][2]['rows'] if r['name'] == 'S3')
        self.assertEqual(row['previous'], 0)
        self.assertEqual(row['delta'], 20)
        self.assertIsNone(row['percent'])

    def test_first_day_has_no_forecast_or_comparison(self):
        result = executive_insights({}, date(2026, 9, 1))
        self.assertIsNone(result['forecast'])
        self.assertIsNone(result['delta'])
        self.assertIsNone(result['mtd'])

    def test_unequal_month_lengths_compare_equal_days(self):
        result = executive_insights({}, date(2026, 3, 31))
        self.assertEqual(result['elapsed'], 28)
        self.assertEqual(result['end'], date(2026, 3, 28))
        self.assertEqual(result['prior_end'], date(2026, 2, 28))

    @override_settings(ENFORCE_CUSTOMER_AUTHORIZATION=True)
    def test_account_scoped_access_cannot_see_other_customer(self):
        other, source = make_customer('Private unrelated customer', '987654321012')
        cost(source, date(2026, 9, 1), 99999)
        access = Access(1, 'scoped-user', False, (self.customer.pk,), (), {self.customer.pk:[self.source.account_id]})
        with context(access):
            result = executive_insights({}, self.today)
            self.assertEqual(result['total'], 150)
            self.assertEqual(len(result['freshness']), 1)
            self.assertEqual(result['freshness'][0]['source_label'], 'Authorized account billing feed')
            self.assertNotIn('source', result['freshness'][0])
            with self.assertRaises(ValueError):
                executive_insights({'customer':str(other.pk)}, self.today)

    def test_view_renders_and_rejects_invalid_metric(self):
        request = RequestFactory().get('/insights/')
        request.user = User.objects.create_user('viewer')
        response = insights(request)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Regional investigation')
        self.assertContains(response, 'Data freshness')
        request = RequestFactory().get('/insights/', {'metric':'injected'})
        request.user = User.objects.get(username='viewer')
        self.assertEqual(insights(request).status_code, 400)

    def test_stale_failed_or_paused_feed_withholds_forecast(self):
        from datetime import timedelta
        for fields in ({'enabled':False}, {'enabled':True, 'last_error':'AccessDenied'},
                       {'last_error':'', 'last_success':timezone.now() - timedelta(days=2)}):
            for key, value in fields.items():
                setattr(self.source, key, value)
            self.source.save()
            result = executive_insights({}, self.today)
            self.assertIsNone(result['forecast'])
