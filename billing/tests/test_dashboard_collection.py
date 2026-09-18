"""Optional dashboard pull controls retain tenant boundaries."""
from unittest.mock import patch
from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.utils import timezone
from billing.models import BillingSource, CustomerMembership, ExplorerQuery, Job
from .helpers import make_customer, web_settings


@web_settings
class DashboardCollectionTests(TestCase):
    def setUp(self):
        self.customer, self.source = make_customer('New account', '123456789012', kind='standalone')
        self.source.initial_import_done = False
        self.source.last_success = None
        self.source.save()
        self.user = User.objects.create_user('collection-operator', is_staff=True)
        self.client.force_login(self.user)

    def test_viewing_ready_connection_never_starts_import_and_manual_pull_coalesces(self):
        with patch('boto3.client', side_effect=AssertionError('The dashboard must not call AWS')):
            page = self.client.get(f'/sources/{self.source.pk}/')
            self.assertEqual(page.status_code, 200)
            self.assertTrue(page.context['ready_to_pull'])
            self.assertFalse(Job.objects.exists())
            for _ in range(2):
                response = self.client.post(f'/sources/{self.source.pk}/import/')
                self.assertEqual(response.status_code, 302)
        job = Job.objects.get(kind='collect')
        self.assertTrue(job.payload['initial'])
        self.assertEqual(job.payload['actor_id'], self.user.pk)
        page = self.client.get(f'/sources/{self.source.pk}/')
        self.assertTrue(page.context['collection_pending'])
        self.assertFalse(page.context['ready_to_pull'])

    def test_unverified_and_paused_connections_cannot_queue_data(self):
        for change in ({'verified_at': None}, {'enabled': False}):
            with self.subTest(change=change):
                BillingSource.objects.filter(pk=self.source.pk).update(verified_at=timezone.now(), enabled=True)
                BillingSource.objects.filter(pk=self.source.pk).update(**change)
                response = self.client.post(f'/sources/{self.source.pk}/import/', follow=True)
                self.assertEqual(response.status_code, 200)
                self.assertFalse(Job.objects.filter(kind__in=['collect', 'import_budgets']).exists())

    def test_bulk_pull_includes_new_and_existing_accounts_and_budget_readers(self):
        _, existing = make_customer('Existing account', '222222222222')
        _, paused = make_customer('Paused account', '333333333333')
        paused.enabled = False
        paused.save()
        _, budget = make_customer('Budget reader', '444444444444', kind=BillingSource.MEMBER_BUDGETS)
        budget.initial_import_done = False
        budget.last_success = None
        budget.approved_capabilities = ['budgets']
        budget.capabilities = {'budgets': True}
        budget.save()
        for _ in range(2):
            self.assertEqual(self.client.post('/sync/pull/').status_code, 302)
        self.assertEqual(set(Job.objects.filter(kind='collect').values_list('source_id', flat=True)), {self.source.pk, existing.pk})
        self.assertEqual(Job.objects.filter(kind='import_budgets').get().source_id, budget.pk)
        self.assertFalse(Job.objects.filter(source=paused).exists())
        page = self.client.get('/activity/')
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context['sync_summary']['pending'], 3)

    def test_pull_endpoints_require_post_and_csrf(self):
        self.assertEqual(self.client.get('/sync/pull/').status_code, 405)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(client.post('/sync/pull/').status_code, 403)

    def test_opening_detailed_report_waits_for_initial_data_collection(self):
        from billing.query_cache import get_query
        with self.assertRaisesMessage(ValueError, 'The initial data pull is preparing or in progress'):
            get_query(self.source, 'get_cost_and_usage', {'TimePeriod': {'Start': '2026-09-01', 'End': '2026-09-02'}})
        self.assertFalse(ExplorerQuery.objects.exists())
        self.assertFalse(Job.objects.exists())

    def test_onboarding_shows_initial_pull_progress_and_job_failure(self):
        job = Job.objects.create(source=self.source, kind='collect', key='automatic-first-pull',
            status=Job.LEASED, payload={'initial': True, 'connection_version': self.source.connection_version})
        response = self.client.get('/onboarding/?state=Pulling+data')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['page'].object_list[0]['state'], 'Pulling data')
        job.status = Job.FAILED
        job.last_error = 'AWS permission needs attention'
        job.save()
        response = self.client.get('/onboarding/?state=Needs+attention')
        self.assertEqual(response.context['page'].object_list[0]['state'], 'Needs attention')
        self.assertContains(response, job.last_error)

    @override_settings(ENFORCE_CUSTOMER_AUTHORIZATION=True)
    def test_bulk_pull_cannot_collect_another_tenants_accounts(self):
        _, other = make_customer('Private account', '222222222222')
        CustomerMembership.objects.create(user=self.user, customer=self.customer, role='operator')
        self.client.force_login(self.user)
        self.assertRedirects(self.client.post('/sync/pull/'), '/activity/', fetch_redirect_response=False)
        self.assertTrue(Job.objects.filter(source=self.source).exists())
        self.assertFalse(Job.objects.filter(source=other).exists())
        response = self.client.get('/activity/')
        self.assertNotContains(response, 'Private account')
        CustomerMembership.objects.filter(user=self.user).update(role='viewer')
        self.client.force_login(self.user)
        self.assertEqual(self.client.post('/sync/pull/').status_code, 403)
