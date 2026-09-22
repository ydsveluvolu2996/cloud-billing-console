from datetime import date
from urllib.parse import parse_qs, urlsplit
import json

from django.contrib.auth.models import User
from django.test import TestCase

from billing import parameters as contract
from billing.access import Access, context
from billing.explorer_workspace import aws_handoff
from billing.models import AccountAssignment
from .helpers import make_customer, web_settings


@web_settings
class WorkspaceTests(TestCase):
    def setUp(self):
        self.customer, self.source = make_customer('Example customer', '111111111111', accounts=('222222222222',))
        self.user = User.objects.create_user('workspace', is_staff=True)
        self.client.force_login(self.user)
        self.params = {'customer': str(self.customer.pk), 'start': '2026-08-01', 'end': '2026-08-31', 'forecast': '0'}

    def filters(self, url):
        return json.loads(parse_qs(urlsplit(url).fragment.split('?', 1)[1])['filter'][0])

    def test_handoff_preserves_customer_scope_and_exclusions(self):
        filters = self.filters(aws_handoff(self.params | {'account': ['222222222222'], 'account_mode': 'exclude'}))
        account = next(f for f in filters if f['dimension']['id'] == 'LinkedAccount')
        self.assertEqual(account['operator'], 'INCLUDES')
        self.assertEqual([v['value'] for v in account['values']], ['111111111111'])
        self.assertNotIn(str(self.customer.pk), aws_handoff(self.params))
        self.assertNotIn('role_arn', aws_handoff(self.params))

    def test_handoff_rejects_multi_connection_and_changing_ownership(self):
        with self.assertRaisesMessage(ValueError, 'Choose one customer'):
            aws_handoff({'date_range': 'last_month'})
        AccountAssignment.objects.filter(customer=self.customer, account__account_id='222222222222').update(end=date(2026, 8, 20))
        with self.assertRaisesMessage(ValueError, 'changing account ownership'):
            aws_handoff(self.params)

    def test_account_restricted_handoff_cannot_widen_selection(self):
        access = Access(self.user.pk, self.user.username, False, (self.customer.pk,), (self.customer.pk,), {self.customer.pk: ['222222222222']})
        with context(access):
            filters = self.filters(aws_handoff(self.params))
        account = next(f for f in filters if f['dimension']['id'] == 'LinkedAccount')
        self.assertEqual([v['value'] for v in account['values']], ['222222222222'])

    def test_recent_reports_are_scoped_and_deduplicated(self):
        first = self.client.get('/', self.params | {'report_name': 'First report'})
        self.assertEqual(first.status_code, 200)
        second = self.client.get('/', self.params | {'report_name': 'Second report'})
        self.assertEqual([r['name'] for r in second.context['recent_reports']], ['First report'])
        third = self.client.get('/', self.params | {'report_name': 'Second report'})
        self.assertEqual([r['name'] for r in third.context['recent_reports']], ['First report'])
        session = self.client.session
        history = session['explorer_recent']
        history['scope'] = 'old-permissions'
        session['explorer_recent'] = history
        session.save()
        response = self.client.get('/', self.params)
        self.assertEqual(response.context['recent_reports'], [])

    def test_presets_keep_account_exclusion_and_rolling_dates(self):
        response = self.client.get('/', self.params | {'account': ['222222222222'], 'account_mode': 'exclude'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['quick_reports']), 7)
        for link in response.context['quick_reports']:
            from django.http import QueryDict
            params = contract.normalize(QueryDict(link['url'].split('?', 1)[1]))
            self.assertEqual(params['customer'], str(self.customer.pk))
            self.assertEqual(params['account'], ['222222222222'])
            self.assertEqual(params['account_mode'], 'exclude')

    def test_new_report_types_explain_external_aws_scope(self):
        response = self.client.get('/reports/new/')
        self.assertContains(response, 'Create cost and usage report')
        self.assertContains(response, 'customer filters are not transferred')
        self.assertEqual(len(response.context['aws_report_types']), 4)
        for report in response.context['aws_report_types']:
            self.assertTrue(report['url'].startswith('https://us-east-1.console.aws.amazon.com/costmanagement/home#/'))
        self.client.logout()
        self.assertEqual(self.client.get('/reports/new/').status_code, 302)
