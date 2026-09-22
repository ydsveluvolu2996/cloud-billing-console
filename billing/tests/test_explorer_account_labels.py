"""Account labels enrich visible rows without changing IDs, filters or tenant boundaries."""
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import parse_qs
from django.contrib.auth.models import User
from django.http import QueryDict
from django.test import TestCase, override_settings
from django.utils import timezone
from billing.access import Access, context
from billing.advanced_explorer import build_report, chart, unpack
from billing.explorer import account_display_labels
from billing.models import AccountAssignment, AwsAccount, ExplorerQuery
from billing.parameters import expression, normalize, querydict
from .helpers import assign, cost, make_customer, web_settings


@web_settings
class ExplorerAccountLabelTests(TestCase):
    def setUp(self):
        self.today = timezone.now().date()
        self.day = self.today - timedelta(days=2)
        self.network_id = '033333333333'
        self.customer, self.source = make_customer('Example customer', '111111111111', accounts=(self.network_id,))
        self.other, self.other_source = make_customer('Another customer', '222222222222')
        AwsAccount.objects.filter(account_id=self.network_id).update(name='Example Network')
        AwsAccount.objects.filter(account_id=self.other_source.account_id).update(name='Private other account')
        self.params = {'date_range': 'custom', 'start': str(self.day), 'end': str(self.day),
                       'group_by': 'account', 'forecast': '0', 'customer': str(self.customer.pk)}
        self.client.force_login(User.objects.create_user('account-labels', is_staff=True))

    def cached_response(self, amount='7'):
        return {'ResultsByTime': [{'TimePeriod': {'Start': str(self.day), 'End': str(self.day+timedelta(days=1))},
            'Groups': [{'Keys': [self.network_id], 'Metrics': {'UnblendedCost': {'Amount': amount, 'Unit': 'USD'}}}]}]}

    def test_local_table_chart_and_export_show_name_and_keep_raw_filter_id(self):
        cost(self.source, self.day, '7', account_id=self.network_id)
        cost(self.source, self.day, '2')  # Unnamed inventory keeps its ID.
        report = build_report(self.params)
        row = next(row for row in report['pivot_rows'] if row['key'] == self.network_id)
        label = f'Example Network ({self.network_id})'
        self.assertEqual(row['label'], label)
        self.assertEqual(row['source_label'], self.network_id)
        self.assertEqual(parse_qs(row['url'].split('?', 1)[1])['account'], [self.network_id])
        self.assertEqual(report['chart_series'][0]['label'], label)
        self.assertEqual(report['pivot_rows'][1]['label'], self.source.account_id)
        self.assertEqual(report['total'], Decimal('9'))
        self.assertContains(self.client.get('/', self.params), label)
        exported = self.client.get('/export/report/', self.params)
        self.assertContains(exported, label)
        self.assertContains(exported, self.network_id)

    def test_cached_account_rows_chart_and_comparison_use_same_labels(self):
        params = normalize(self.params | {'granularity': 'daily', 'report_mode': 'compare'})
        rows, _, unit = unpack([SimpleNamespace(data=self.cached_response())], params, [str(self.day)])
        self.assertEqual(rows[0]['key'], self.network_id)
        self.assertEqual(rows[0]['label'], f'Example Network ({self.network_id})')
        payload, totals = chart(rows, [str(self.day)], params, unit)
        self.assertEqual(payload['series'][0]['label'], rows[0]['label'])
        self.assertEqual(totals, [Decimal('7')])
        build_report(params)
        for query in ExplorerQuery.objects.all():
            response = self.cached_response()
            response['ResultsByTime'][0]['TimePeriod'] = query.parameters['TimePeriod']
            query.data = response
            query.requested = False
            query.last_attempt = query.last_success = timezone.now()
            query.save()
        compared = build_report(params)
        self.assertEqual(compared['comparison_rows'][0]['label'], rows[0]['label'])
        self.assertEqual(compared['comparison_chart']['periods'], [rows[0]['label']])
        self.assertEqual(compared['comparison_rows'][0]['key'], self.network_id)

    @override_settings(ENFORCE_CUSTOMER_AUTHORIZATION=True)
    def test_names_follow_tenant_and_account_restrictions(self):
        AwsAccount.objects.filter(account_id=self.source.account_id).update(name='Private same-customer account')
        access = Access(1, 'restricted-reader', False, (self.customer.pk,), (), {self.customer.pk: [self.network_id]})
        with context(access):
            labels = account_display_labels([self.network_id, self.source.account_id, self.other_source.account_id])
        self.assertEqual(labels[self.network_id], f'Example Network ({self.network_id})')
        self.assertEqual(labels[self.source.account_id], self.source.account_id)
        self.assertEqual(labels[self.other_source.account_id], self.other_source.account_id)
        self.assertNotIn('Private', ' '.join(labels.values()))

    @override_settings(ENFORCE_CUSTOMER_AUTHORIZATION=True)
    def test_former_owner_gets_historical_id_without_new_owner_name(self):
        cost(self.source, self.day, '7', account_id=self.network_id)
        AccountAssignment.objects.filter(account__account_id=self.network_id, end__isnull=True).update(end=self.today)
        assign(self.network_id, self.other, self.source, start=self.today)
        AwsAccount.objects.filter(account_id=self.network_id).update(name='New owner private name')
        access = Access(1, 'historical-reader', False, (self.customer.pk,), (), {})
        with context(access):
            self.assertEqual(account_display_labels([self.network_id], self.customer.pk), {self.network_id: self.network_id})
            rows, _, _ = unpack([SimpleNamespace(data=self.cached_response())], normalize(self.params), [self.day.strftime('%b %Y')])
            self.assertEqual(rows[0]['label'], self.network_id)
        # A portfolio operator selecting the historical customer receives the same fallback.
        self.assertEqual(account_display_labels([self.network_id], self.customer.pk), {self.network_id: self.network_id})
        self.assertEqual(account_display_labels([self.network_id], self.other.pk)[self.network_id], f'New owner private name ({self.network_id})')

    def test_account_whitespace_trims_before_dedup_and_aws_include_exclude(self):
        raw = QueryDict('account=%20033333333333%20&account=033333333333&account=%09&account_mode=exclude')
        params = normalize(raw)
        self.assertEqual(params['account'], [self.network_id])
        self.assertEqual(expression(params), {'Not': {'Dimensions': {'Key': 'LINKED_ACCOUNT', 'Values': [self.network_id]}}})
        self.assertEqual(normalize(querydict(params))['account'], [self.network_id])
        included = normalize(self.params | {'account': ['  '+self.network_id+'\t'], 'region': ['ap-south-1']})
        self.assertIn({'Dimensions': {'Key': 'LINKED_ACCOUNT', 'Values': [self.network_id]}}, expression(included)['And'])
        # Whitespace may be meaningful in exact tag/service values; only account IDs change.
        tagged = normalize(self.params | {'tag_key': 'Team', 'tag': [' DEV '], 'service': [' Service ']})
        self.assertEqual(tagged['tag'], [' DEV '])
        self.assertEqual(tagged['service'], [' Service '])
