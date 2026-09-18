from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from billing.collection_state import snapshot
from billing.models import CustomerMembership, Job
from .helpers import TEST_STORAGES, assign, make_customer


@override_settings(MFA_REQUIRED=False, ENFORCE_CUSTOMER_AUTHORIZATION=True, REQUIRE_CONNECTION_APPROVAL=False,
                   SECURE_SSL_REDIRECT=False, STORAGES=TEST_STORAGES, ALLOWED_HOSTS=['testserver'])
class CollectionVisibilityTests(TestCase):
    def setUp(self):
        self.customer, self.source = make_customer('Payer owner', '111111111111', accounts=['222222222222'])
        self.source.last_error = 'Private payer connection diagnostic'
        self.source.save(update_fields=['last_error'])
        self.user = User.objects.create_user('scoped-activity-user')

    def test_member_restricted_viewer_cannot_see_payer_connection_metadata(self):
        CustomerMembership.objects.create(user=self.user, customer=self.customer, role='viewer', account_ids=['222222222222'])
        self.client.force_login(self.user)
        response = self.client.get('/activity/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['collection_rows'], [])
        self.assertNotContains(response, self.source.account_id)
        self.assertNotContains(response, self.source.last_error)
        self.assertFalse(response.context['can_refresh_all'])

    def test_shared_member_customer_cannot_see_other_customers_payer_metadata(self):
        self.source.shared = True
        self.source.save(update_fields=['shared'])
        member, _ = make_customer('Member customer', '333333333333', connected=False)
        assign('444444444444', member, self.source)
        CustomerMembership.objects.create(user=self.user, customer=member, role='viewer')
        self.client.force_login(self.user)
        response = self.client.get('/activity/')
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, self.customer.name)
        self.assertNotContains(response, self.source.account_id)
        self.assertNotContains(response, self.source.last_error)

    def test_whole_customer_operator_keeps_collection_controls(self):
        CustomerMembership.objects.create(user=self.user, customer=self.customer, role='operator')
        self.client.force_login(self.user)
        response = self.client.get('/activity/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.source.account_id)
        self.assertTrue(response.context['can_refresh_all'])
        self.assertTrue(response.context['collection_rows'][0]['can_pull'])


@override_settings(REQUIRE_CONNECTION_APPROVAL=False)
class CurrentCollectionStatusTests(TestCase):
    def setUp(self):
        _, self.source = make_customer('Current connection', '123456789012')
        self.source.connection_version = 2
        self.source.save(update_fields=['connection_version'])

    def job(self, kind, status, version=None, error=''):
        payload = {'connection_version': version} if version is not None else {}
        return Job.objects.create(source=self.source, kind=kind, key=f'fixture-{Job.objects.count()}',
            status=status, payload=payload, last_error=error)

    def test_old_version_and_unselected_budget_jobs_do_not_block_current_connection(self):
        jobs = [self.job('collect', Job.QUEUED, 1),
                self.job('collect', Job.FAILED, 1, 'Old role failed'),
                self.job('import_budgets', Job.FAILED, 2, 'Budget permission removed')]
        state = snapshot(self.source, jobs, can_edit=True)
        self.assertFalse(state['pending'])
        self.assertEqual(state['error'], '')
        self.assertEqual(state['status'], 'Automatic refresh enabled')
        self.assertTrue(state['can_pull'])

    def test_legacy_versionless_jobs_and_current_selected_budget_jobs_remain_visible(self):
        legacy = self.job('collect', Job.QUEUED)
        self.assertTrue(snapshot(self.source, [legacy])['pending'])
        self.source.approved_capabilities = ['budgets']
        self.source.save(update_fields=['approved_capabilities'])
        failed = self.job('import_budgets', Job.FAILED, 2, 'Current budget error')
        state = snapshot(self.source, [failed])
        self.assertEqual(state['status'], 'Needs attention')
        self.assertEqual(state['error'], 'Current budget error')

    def test_failed_first_pull_shows_actionable_failure_and_remains_retryable(self):
        self.source.initial_import_done = False
        self.source.last_success = None
        self.source.last_error = 'Older general error'
        self.source.save(update_fields=['initial_import_done', 'last_success', 'last_error'])
        failed = self.job('collect', Job.FAILED, 2, 'Current collection needs permission')
        state = snapshot(self.source, [failed], can_edit=True)
        self.assertEqual(state['status'], 'Needs attention')
        self.assertEqual(state['error'], failed.last_error)
        self.assertFalse(state['ready_to_pull'])
        self.assertTrue(state['can_pull'])
