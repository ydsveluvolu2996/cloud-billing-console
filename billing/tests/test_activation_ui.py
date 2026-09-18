"""Dashboard onboarding boundaries and the customer administrator handoff."""
import json
from django.conf import settings
from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.utils import timezone
from billing.forms import SourceForm
from billing.models import ActivationRequest, BillingSource, CustomerApproval, CustomerMembership, Job, RoleApproval, UserSecurity
from billing.tests.helpers import make_customer, TEST_STORAGES


@override_settings(SECURE_SSL_REDIRECT=False, STORAGES=TEST_STORAGES, ALLOWED_HOSTS=['testserver', 'localhost'],
                   MFA_REQUIRED=True, ENFORCE_CUSTOMER_AUTHORIZATION=True, REQUIRE_CONNECTION_APPROVAL=True,
                   COLLECTOR_ROLE_ARN='arn:aws:iam::111111111111:role/Collector',
                   ONBOARDING_BROKER_FUNCTION='test-onboarding-broker')
class ActivationUiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        if connection.vendor == 'postgresql':
            # Production installs the guarded request function with the runtime
            # policies. Install it before fixtures create deferred FK events.
            with connection.cursor() as cursor:
                cursor.execute((settings.BASE_DIR / 'deploy/database-roles.sql').read_text())

    def setUp(self):
        self.database_settings = override_settings(DATABASE_RLS_ENABLED=connection.vendor == 'postgresql')
        self.database_settings.enable()
        self.addCleanup(self.database_settings.disable)
        self.customer, self.source = make_customer('Single member customer', '123456789012', kind='standalone', connected=False)
        self.source.role_arn = self.source.expected_role_arn
        self.source.approved_capabilities = ['budgets']
        self.source.save()
        self.admin = User.objects.create_superuser('portfolio-admin', email='owner@example.test', password='test-only-a-long-password')
        UserSecurity.objects.update_or_create(user=self.admin, defaults={'portfolio_access': True})
        self.sign_in(self.admin)
        self.url = f'/sources/{self.source.pk}/'
        self.consent = {'action': 'activate', 'contact': 'Account owner', 'evidence': 'Account owner approved read-only billing import',
                        'retention_days': '365', 'confirmed': 'on'}

    def sign_in(self, user, client=None):
        client = client or self.client
        client.force_login(user)
        profile, _ = UserSecurity.objects.get_or_create(user=user)
        session = client.session
        session['mfa_at'] = timezone.now().timestamp()
        session['security_version'] = profile.session_version
        session.save()

    def test_new_connection_defaults_to_single_account_with_budget_reads(self):
        form = SourceForm()
        self.assertEqual(form['kind'].value(), BillingSource.STANDALONE)
        self.assertTrue(form['import_budgets'].value())
        page = self.client.get(f'/customers/{self.customer.pk}/sources/add/')
        self.assertContains(page, 'Single AWS account (including member)')
        self.assertContains(page, 'You do not need access to its organization')

    def test_save_role_does_not_queue_unapproved_verification(self):
        response = self.client.post(self.url, {'action': 'connection', 'role_arn': self.source.role_arn, 'approved_capabilities': ['budgets']})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Job.objects.filter(source=self.source).exists())
        self.assertTrue(RoleApproval.objects.filter(source=self.source, status='requested').exists())
        self.client.post(f'/sources/{self.source.pk}/verify/')
        self.assertFalse(Job.objects.filter(source=self.source, kind='verify').exists())

    def test_options_can_be_saved_before_customer_role_exists(self):
        self.source.role_arn = ''
        self.source.save()
        response = self.client.post(self.url, {'action': 'connection', 'role_arn': '', 'approved_capabilities': ['budgets', 'forecasts']})
        self.assertEqual(response.status_code, 302)
        self.source.refresh_from_db()
        self.assertEqual(self.source.approved_capabilities, ['budgets', 'forecasts'])
        self.assertFalse(RoleApproval.objects.filter(source=self.source).exists())
        self.assertFalse(Job.objects.filter(source=self.source).exists())

    def test_single_account_rejects_organization_inventory_and_shared_payer(self):
        response = self.client.post(self.url, {'action': 'connection', 'role_arn': self.source.role_arn, 'approved_capabilities': ['organizations']})
        self.assertContains(response, 'Select a valid choice')
        form = SourceForm({'kind': 'standalone', 'account_id': '222222222222', 'shared': 'on'})
        self.assertFalse(form.is_valid())
        self.assertIn('shared', form.errors)

    def test_combined_policy_includes_selected_budgets_only(self):
        response = self.client.get(f'/sources/{self.source.pk}/setup/')
        self.assertEqual(response.status_code, 200)
        policy = json.loads(response.context['permission_json'])
        actions = [action for statement in policy['Statement'] for action in (statement['Action'] if isinstance(statement['Action'], list) else [statement['Action']])]
        self.assertIn('budgets:ViewBudget', actions)
        self.assertIn('ce:GetCostAndUsage', actions)
        self.assertNotIn('organizations:ListAccounts', actions)
        self.assertNotIn('ce:GetTags', actions)
        self.assertContains(response, '--path /BillingConsole/')
        self.assertContains(response, '--role-name CostReadOnly')
        download = self.client.get(f'/sources/{self.source.pk}/template/')
        self.assertEqual(json.loads(download.content)['permission_policy'], policy)

    def test_connect_queues_once_and_does_not_directly_approve_or_verify(self):
        for _ in range(2):
            response = self.client.post(self.url, self.consent)
            self.assertEqual(response.status_code, 302)
        activation = ActivationRequest.objects.get(source=self.source)
        self.assertEqual(activation.status, 'queued')
        self.assertEqual(activation.snapshot['source']['account_id'], '123456789012')
        self.assertEqual(activation.snapshot['consent']['retention_days'], 365)
        self.assertFalse(CustomerApproval.objects.filter(customer=self.customer, status='approved').exists())
        self.assertFalse(Job.objects.filter(source=self.source, kind='verify').exists())
        page = self.client.get(self.url)
        self.assertContains(page, 'Waiting to connect')
        self.assertContains(page, 'Connection checks are already running')

    def test_consent_is_required_and_no_request_is_written(self):
        self.consent.pop('confirmed')
        response = self.client.post(self.url, self.consent)
        self.assertContains(response, 'This field is required')
        self.assertFalse(ActivationRequest.objects.exists())

    def test_operator_and_viewer_cannot_activate(self):
        for role in ('operator', 'viewer'):
            user = User.objects.create_user(role, password='test-only-a-long-password', is_staff=True)
            CustomerMembership.objects.create(user=user, customer=self.customer, role=role)
            self.sign_in(user)
            self.assertEqual(self.client.post(self.url, self.consent).status_code, 403)
        self.assertFalse(ActivationRequest.objects.exists())

    @override_settings(EXTERNAL_PORTAL_ENABLED=True)
    def test_external_superuser_cannot_activate(self):
        UserSecurity.objects.filter(user=self.admin).update(external=True)
        CustomerMembership.objects.create(user=self.admin, customer=self.customer, role='customer')
        self.sign_in(self.admin)
        self.assertEqual(self.client.post(self.url, self.consent).status_code, 403)
        self.assertFalse(ActivationRequest.objects.exists())

    def test_expired_mfa_cannot_activate(self):
        session = self.client.session
        session['mfa_at'] = 0
        session.save()
        self.assertEqual(self.client.post(self.url, self.consent).status_code, 302)
        self.assertFalse(ActivationRequest.objects.exists())

    def test_activation_requires_csrf(self):
        client = Client(enforce_csrf_checks=True)
        self.sign_in(self.admin, client=client)
        self.assertEqual(client.post(self.url, self.consent).status_code, 403)
        self.assertFalse(ActivationRequest.objects.exists())

    def test_failed_activation_is_actionable_in_connection_and_onboarding(self):
        self.client.post(self.url, self.consent)
        ActivationRequest.objects.filter(source=self.source).update(status='failed', last_error='Customer role trust does not match. Copy the current trust policy and retry.')
        response = self.client.get(self.url)
        self.assertContains(response, 'Needs attention')
        self.assertContains(response, 'Copy the current trust policy')
        self.assertContains(response, 'Retry connection')
        response = self.client.get('/onboarding/?state=Needs+attention')
        self.assertContains(response, self.customer.name)
        self.assertContains(response, 'Copy the current trust policy')

    def test_completed_setup_does_not_hide_paused_or_changed_connection(self):
        self.client.post(self.url, self.consent)
        ActivationRequest.objects.filter(source=self.source).update(status='completed')
        self.source.verified_at = timezone.now()
        self.source.initial_import_done = True
        self.source.last_success = timezone.now()
        self.source.enabled = False
        self.source.save()
        response = self.client.get(self.url)
        self.assertEqual(response.context['activation_status']['label'], 'Paused')
        self.source.enabled = True
        self.source.connection_version += 1
        self.source.verified_at = None
        self.source.save()
        response = self.client.get(self.url)
        self.assertEqual(response.context['activation_status']['label'], 'Awaiting customer setup')

    def test_payer_connection_explains_reviewed_workflow(self):
        self.source.kind = BillingSource.PAYER
        self.source.save()
        response = self.client.get(self.url)
        self.assertContains(response, 'Management and shared payer connections require reviewed authorization')
        self.assertNotContains(response, 'name="action" value="activate"')

    @override_settings(ONBOARDING_BROKER_FUNCTION='')
    def test_unconfigured_automation_is_explicit_and_does_not_enqueue(self):
        self.assertContains(self.client.get(self.url), 'Automatic connection setup is not available yet')
        self.client.post(self.url, self.consent)
        self.assertFalse(ActivationRequest.objects.exists())
