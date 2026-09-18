import csv
import io
from datetime import timedelta
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

from dateutil.relativedelta import relativedelta
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone

from billing.models import ExplorerQuery
from .helpers import cost, make_customer, web_settings


class Controls(HTMLParser):
    def __init__(self, content):
        super().__init__()
        self.elements = []
        self.current = None
        self.feed(content.decode())

    def handle_starttag(self, tag, attrs):
        if tag in ('a', 'button'):
            self.current = {'tag': tag, 'attrs': dict(attrs), 'text': ''}

    def handle_data(self, data):
        if self.current is not None:
            self.current['text'] += data

    def handle_endtag(self, tag):
        if self.current is not None and tag == self.current['tag']:
            self.elements.append(self.current)
            self.current = None

    def named(self, text):
        return next(item for item in self.elements if item['text'].strip() == text)


@web_settings
class CustomerMonthExportTests(TestCase):
    def setUp(self):
        self.customer, self.source = make_customer('Selected customer', '012345678901', currency='EUR')
        self.client.force_login(User.objects.create_user('month-export-reader'))
        self.today = timezone.now().date()
        self.month = self.today.replace(day=1) - relativedelta(months=1)
        self.end = self.today.replace(day=1) - timedelta(days=1)

    def test_historical_month_export_matches_every_customer_tab(self):
        cost(self.source, self.month, '10', service='Selected start', currency='EUR')
        cost(self.source, self.end, '20', service='Selected end', currency='EUR')
        cost(self.source, self.month - timedelta(days=1), '30', service='Earlier month', currency='EUR')
        cost(self.source, self.today, '40', service='Later month', currency='EUR')
        cost(self.source, self.month, '50', service='Different currency')
        _, other_source = make_customer('Another customer', '111111111111')
        cost(other_source, self.month, '60', service='Other customer', currency='EUR')
        for tab in ('accounts', 'projects', 'budgets', 'reports', 'sync'):
            with self.subTest(tab=tab):
                page = self.client.get(f'/customers/{self.customer.pk}/', {'month': self.month.strftime('%Y-%m'), 'tab': tab, 'currency': 'EUR'})
                self.assertEqual(page.status_code, 200)
                url = Controls(page.content).named('↓ Export CSV')['attrs']['href']
                params = parse_qs(urlsplit(url).query)
                self.assertEqual(params['start'], [str(self.month)])
                self.assertEqual(params['end'], [str(self.end)])
                self.assertEqual(params['customer'], [str(self.customer.pk)])
                self.assertEqual(params['currency'], ['EUR'])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        rows = list(csv.reader(io.StringIO(response.content.decode())))
        facts = [row for row in rows[1:] if len(row) == 9]
        self.assertEqual([(row[0], row[4], row[5]) for row in facts], [
            (str(self.month), 'Selected start', 'EUR'), (str(self.end), 'Selected end', 'EUR')])

    def test_current_month_export_stops_at_today(self):
        cost(self.source, self.today, '10', currency='EUR')
        response = self.client.get(f'/customers/{self.customer.pk}/', {'tab': 'reports'})
        url = Controls(response.content).named('↓ Export CSV')['attrs']['href']
        self.assertEqual(parse_qs(urlsplit(url).query)['end'], [str(self.today)])
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_future_months_fall_back_to_current_month_on_every_tab(self):
        for tab in ('accounts', 'projects', 'budgets', 'reports', 'sync'):
            with self.subTest(tab=tab):
                response = self.client.get(f'/customers/{self.customer.pk}/', {'month': '9999-12', 'tab': tab})
                self.assertEqual(response.status_code, 200)
                url = Controls(response.content).named('↓ Export CSV')['attrs']['href']
                params = parse_qs(urlsplit(url).query)
                self.assertEqual(params['start'], [str(self.today.replace(day=1))])
                self.assertEqual(params['end'], [str(self.today)])


@web_settings
class ExplorerExportAvailabilityTests(TestCase):
    def setUp(self):
        self.customer, self.source = make_customer('Report customer', '012345678901')
        self.client.force_login(User.objects.create_user('availability-reader'))
        self.day = timezone.now().date() - timedelta(days=2)
        self.params = {'start': str(self.day), 'end': str(self.day), 'date_range': 'custom',
                       'group_by': 'region', 'customer': str(self.customer.pk), 'forecast': '0'}

    def assert_export_controls(self, response, enabled):
        controls = Controls(response.content)
        for label in ('Export report ↓', 'Download as CSV ↓'):
            control = controls.named(label)
            self.assertEqual(control['tag'], 'a' if enabled else 'button')
            self.assertEqual('disabled' not in control['attrs'], enabled)
            if not enabled:
                self.assertEqual(control['attrs']['aria-describedby'], 'report-export-status')

    def complete_query(self):
        self.client.get('/', self.params)
        query = ExplorerQuery.objects.get()
        query.data = {'ResultsByTime': [{'TimePeriod': {'Start': str(self.day), 'End': str(self.day + timedelta(days=1))},
            'Groups': [{'Keys': ['us-east-1'], 'Metrics': {'UnblendedCost': {'Amount': '7', 'Unit': 'USD'}}}]}]}
        query.requested = False
        query.last_attempt = query.last_success = timezone.now()
        query.save()
        return query

    def test_pending_results_disable_links_and_direct_export_explains_progress(self):
        page = self.client.get('/', self.params)
        self.assert_export_controls(page, enabled=False)
        response = self.client.get('/export/report/', self.params)
        self.assertEqual(response.status_code, 409)
        self.assertNotIn('Content-Disposition', response)
        self.assertContains(response, 'Collection is still running', status_code=409)
        url = Controls(response.content).named('Return to this report')['attrs']['href']
        params = parse_qs(urlsplit(url).query)
        for key in ('start', 'end', 'customer', 'group_by'):
            self.assertEqual(params[key], [self.params[key]])

    def test_failed_results_explain_required_action_without_promising_completion(self):
        self.client.get('/', self.params)
        ExplorerQuery.objects.update(requested=False, last_attempt=timezone.now(), error='Billing permission missing.')
        page = self.client.get('/', self.params)
        self.assert_export_controls(page, enabled=False)
        response = self.client.get('/export/report/', self.params)
        self.assertContains(response, 'Billing permission missing.', status_code=409)
        self.assertContains(response, 'check the affected customer connections', status_code=409)
        self.assertNotContains(response, 'Collection is still running', status_code=409)

    def test_complete_results_and_cached_results_being_refreshed_remain_exportable(self):
        query = self.complete_query()
        for refreshing in (False, True):
            with self.subTest(refreshing=refreshing):
                if refreshing:
                    query.last_attempt = timezone.now() - timedelta(hours=7)
                    query.save(update_fields=['last_attempt'])
                page = self.client.get('/', self.params)
                self.assert_export_controls(page, enabled=True)
                self.assertEqual(page.context['report_pending'], refreshing)
                response = self.client.get('/export/report/', self.params)
                self.assertEqual(response.status_code, 200)
                self.assertIn('attachment;', response['Content-Disposition'])
                rows = list(csv.reader(io.StringIO(response.content.decode())))
                self.assertEqual(rows[1][1], '7')

    @override_settings(REQUIRE_CONNECTION_APPROVAL=True)
    def test_unavailable_optional_capability_disables_export_with_its_reason(self):
        params = self.params | {'group_by': 'tag', 'group_key': 'Team'}
        page = self.client.get('/', params)
        self.assert_export_controls(page, enabled=False)
        response = self.client.get('/export/report/', params)
        self.assertContains(response, 'requires customer approval', status_code=409)
        self.assertNotContains(response, 'Collection is still running', status_code=409)
