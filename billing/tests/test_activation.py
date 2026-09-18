import io
import json
import os
import stat
from pathlib import Path
from tempfile import TemporaryDirectory
import secrets
from unittest import skipUnless
from unittest.mock import patch
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from billing.activation import request_activation, process_activation, merge_allowlist, source_snapshot, allow_role_via_broker
from billing.models import ActivationRequest, CustomerApproval, RoleApproval, UserSecurity, BillingSource, Job
from billing.tests.helpers import make_customer


@override_settings(ONBOARDING_BROKER_FUNCTION='test-broker', RUNTIME_ROLE='admin', AWS_REGION='ap-south-1',
                   COLLECTOR_ROLE_ARN='arn:aws:iam::999999999999:role/CloudBillingCollector')
class ActivationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        if connection.vendor == 'postgresql':
            # Install production capabilities before creating any rows. Django
            # defers FK triggers inside TestCase; ALTER TABLE after fixtures is
            # correctly rejected by PostgreSQL while those events are pending.
            with connection.cursor() as cursor:
                cursor.execute((Path(__file__).parents[2] / 'deploy/database-roles.sql').read_text())

    def setUp(self):
        self.user = User.objects.create_superuser('activation-admin', password='test-only-activation-password')
        self.profile = UserSecurity.objects.create(user=self.user, portfolio_access=True)
        self.customer, self.source = make_customer('Activation customer', '123456789012', kind='standalone', connected=False)
        self.source.role_arn = self.source.expected_role_arn
        self.source.approved_capabilities = ['budgets']
        self.source.save()
        self.consent = {'contact': 'Customer finance contact', 'evidence': 'Customer authorization ticket 123', 'retention_days': 365, 'confirmed': True}
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'approved-roles.json'
        self.old_arn = 'arn:aws:iam::222222222222:role/BillingConsole/CostReadOnly'
        self.path.write_text(json.dumps([self.old_arn]))
        self.path.chmod(0o640)
        self.settings_override = override_settings(COLLECTOR_ALLOWLIST_FILE=str(self.path))
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)

    def request(self, user=None, **kwargs):
        user = user or self.user
        if connection.vendor == 'postgresql':
            with connection.cursor() as cursor:
                cursor.execute("SELECT set_config('billing.user_id',%s,true),set_config('billing.mfa_verified','true',true)", [str(user.pk)])
        return request_activation(self.source, user, kwargs.pop('consent', self.consent),
            mfa_verified=kwargs.pop('mfa_verified', True), session_version=kwargs.pop('session_version', self.profile.session_version), **kwargs)

    def activate(self):
        request = self.request()
        with patch('billing.activation.allow_role_via_broker') as broker:
            process_activation(request)
            broker.assert_called_once_with(self.source.role_arn)
        request.refresh_from_db()
        self.assertEqual(request.status, 'verifying', request.last_error)
        return request

    def finish_verification(self, request, discover=True):
        self.source.trust_checks = {'correct_external_id':'passed','missing_external_id':'denied','wrong_external_id':'denied',
            'account_identity':'passed','exact_collector_principal':'passed','connection_version':self.source.connection_version}
        self.source.verified_at = timezone.now()
        self.source.discovered_at = timezone.now() if discover else None
        self.source.capabilities = {'budgets': True}
        self.source.save()
        if not discover:
            from billing.scheduler import request_discovery
            request_discovery(self.source)
        Job.objects.filter(key=f'activation:{request.pk}:verify').update(status=Job.DONE)

    def test_mfa_and_live_portfolio_session_required(self):
        for kwargs in ({'mfa_verified': False}, {'session_version': 99}):
            with self.assertRaises(PermissionDenied):
                self.request(**kwargs)
        for field, value in (('external', True), ('portfolio_access', False)):
            old = getattr(self.profile, field)
            setattr(self.profile, field, value)
            self.profile.save()
            with self.assertRaises(PermissionDenied):
                self.request()
            setattr(self.profile, field, old)
            self.profile.save()
        self.user.is_superuser = False
        self.user.save()
        with self.assertRaises(PermissionDenied):
            self.request()

    def test_request_is_idempotent_and_contains_no_raw_external_id(self):
        request = self.request()
        self.assertEqual(self.request().pk, request.pk)
        self.assertNotIn(self.source.external_id, json.dumps(request.snapshot))
        self.assertEqual(request.snapshot['source'], source_snapshot(self.source))
        self.assertFalse(CustomerApproval.objects.filter(status='approved').exists())
        self.assertFalse(RoleApproval.objects.filter(status='approved').exists())

    def test_reject_broad_or_mismatched_role_and_missing_consent(self):
        for arn in ('arn:aws:iam::123456789012:role/Admin', 'arn:aws:iam::123456789012:role/BillingConsole/*',
                    'arn:aws:iam::111111111111:role/BillingConsole/CostReadOnly'):
            self.source.role_arn = arn
            with self.assertRaises(ValidationError):
                self.request()
        self.source.role_arn = self.source.expected_role_arn
        for change in ({'confirmed': False}, {'contact': ''}, {'evidence': ''}, {'retention_days': 0}, {'retention_days': True}):
            with self.assertRaises(ValidationError):
                self.request(consent=dict(self.consent, **change))

    def test_actor_revoked_before_execution_never_calls_broker(self):
        request = self.request()
        self.profile.session_version += 1
        self.profile.save()
        with patch('billing.activation.allow_role_via_broker') as broker:
            process_activation(request)
            broker.assert_not_called()
        request.refresh_from_db()
        self.assertEqual(request.status, 'failed')
        self.assertEqual(json.loads(self.path.read_text()), [self.old_arn])

    def test_source_or_consent_changed_before_execution_never_calls_broker(self):
        request = self.request()
        self.source.external_id = 'changed-external-id'
        self.source.save()
        with patch('billing.activation.allow_role_via_broker') as broker:
            process_activation(request)
            broker.assert_not_called()
        request.refresh_from_db()
        self.assertEqual(request.status, 'failed')

    def test_cloud_failure_preserves_no_approval_and_retries(self):
        request = self.request()
        with patch('billing.activation.allow_role_via_broker', side_effect=RuntimeError('private AWS diagnostic')):
            process_activation(request)
        request.refresh_from_db()
        self.assertEqual(request.status, 'queued')
        self.assertNotIn('private', request.last_error)
        self.assertFalse(RoleApproval.objects.filter(status='approved').exists())
        self.assertFalse(Job.objects.exists())
        self.assertEqual(json.loads(self.path.read_text()), [self.old_arn])

    def test_configuration_change_during_broker_is_rechecked(self):
        request = self.request()
        def change(arn):
            BillingSource.objects.filter(pk=self.source.pk).update(connection_version=9)
        with patch('billing.activation.allow_role_via_broker', side_effect=change):
            process_activation(request)
        request.refresh_from_db()
        self.assertEqual(request.status, 'failed')
        self.assertFalse(RoleApproval.objects.filter(status='approved').exists())
        self.assertEqual(json.loads(self.path.read_text()), [self.old_arn])

    def test_allowlist_metadata_and_existing_approvals_preserved(self):
        self.activate()
        self.assertEqual(set(json.loads(self.path.read_text())), {self.old_arn, self.source.role_arn})
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o640)
        approval = CustomerApproval.objects.get(customer=self.customer)
        self.assertEqual(approval.expected_accounts, [self.source.account_id])
        self.assertEqual(approval.authorized_users, [self.user.username])
        self.assertFalse(Job.objects.filter(kind='collect').exists())

    def test_connect_requires_fresh_verification_and_discovery_without_importing(self):
        request = self.activate()
        self.finish_verification(request, discover=False)
        process_activation(request)
        request.refresh_from_db()
        self.assertEqual(request.status, 'verifying')
        self.assertFalse(Job.objects.filter(kind='collect').exists())
        self.source.discovered_at = timezone.now()
        self.source.save()
        process_activation(request)
        request.refresh_from_db()
        self.assertEqual(request.status, 'completed')
        self.assertIsNotNone(request.finished_at)
        self.assertFalse(Job.objects.filter(kind__in=['collect', 'import_budgets']).exists())
        self.source.refresh_from_db()
        self.assertFalse(self.source.initial_import_done)

    def test_existing_inflight_import_finishes_after_upgrade(self):
        request = self.activate()
        self.finish_verification(request)
        request.status = 'importing'
        request.save()
        Job.objects.create(source=self.source, kind='collect', key=f'activation:{request.pk}:initial', status=Job.DONE)
        budget = Job.objects.create(source=self.source, kind='import_budgets', key=f'activation:{request.pk}:budgets')
        process_activation(request)
        request.refresh_from_db()
        self.assertEqual(request.status, 'importing')
        budget.status = Job.DONE
        budget.save()
        process_activation(request)
        request.refresh_from_db()
        self.assertEqual(request.status, 'completed')

    def test_revoked_role_approval_is_not_automatically_reapproved(self):
        request = self.request()
        RoleApproval.objects.create(source=self.source, role_arn=self.source.role_arn,
            connection_version=self.source.connection_version, status='revoked', requested_by='admin', evidence='Revoked')
        with patch('billing.activation.allow_role_via_broker') as broker:
            process_activation(request)
            broker.assert_not_called()
        request.refresh_from_db()
        self.assertEqual(request.status, 'failed')
        self.assertEqual(RoleApproval.objects.get(source=self.source).status, 'revoked')
        with self.assertRaises(ValidationError):
            self.request()

    def test_selected_budget_permission_failure_cannot_report_connected(self):
        request = self.activate()
        self.finish_verification(request)
        self.source.capabilities = {'budgets': False}
        self.source.save()
        process_activation(request)
        request.refresh_from_db()
        self.assertEqual(request.status, 'failed')
        self.assertIn('budget access is unavailable', request.last_error)
        self.assertFalse(Job.objects.filter(kind__in=['collect', 'import_budgets']).exists())

    def test_failed_verification_does_not_import(self):
        request = self.activate()
        Job.objects.filter(key=f'activation:{request.pk}:verify').update(status=Job.FAILED, last_error='Trust policy did not pass')
        process_activation(request)
        request.refresh_from_db()
        self.assertEqual(request.status, 'failed')
        self.assertFalse(Job.objects.filter(kind='collect').exists())

    def test_queued_verification_remains_pending(self):
        request = self.activate()
        process_activation(request)
        request.refresh_from_db()
        self.assertEqual(request.status, 'verifying')
        self.assertEqual(request.last_error, '')

    def test_pause_resume_reports_missing_required_jobs_in_each_phase(self):
        from billing.onboarding import set_paused
        for phase in ('verification', 'discovery'):
            with self.subTest(phase=phase):
                request = self.activate()
                if phase == 'discovery':
                    self.finish_verification(request, discover=False)
                # Pause removes queued jobs. Resuming before the coordinator
                # polls must not leave an otherwise valid request waiting forever.
                set_paused(self.source, True)
                set_paused(self.source, False)
                process_activation(request)
                request.refresh_from_db()
                self.assertEqual(request.status, 'failed')
                self.assertIn('interrupted', request.last_error)
                self.assertIn('Connect account', request.last_error)

    def test_existing_customer_consent_is_appended_not_replaced(self):
        CustomerApproval.objects.create(customer=self.customer, contacts=['Original contact'], authorized_users=['original'],
            expected_accounts=['222222222222'], billing_fields=['cost'], optional_capabilities=['forecasts'], metadata=['alias'],
            storage_region='ap-south-1', retention_days=365, status='approved', evidence='Original approval',
            approved_by='original', approved_at=timezone.now())
        self.activate()
        approval = CustomerApproval.objects.get(customer=self.customer)
        self.assertEqual(approval.contacts, ['Original contact'])
        self.assertEqual(approval.evidence, 'Original approval')
        self.assertEqual(approval.metadata, ['alias'])
        self.assertEqual(set(approval.expected_accounts), {'222222222222', self.source.account_id})
        self.assertEqual(set(approval.optional_capabilities), {'forecasts', 'budgets'})

    def test_unsafe_allowlist_never_replaced(self):
        self.path.chmod(0o666)
        with self.assertRaises(ValueError):
            merge_allowlist(self.source.role_arn)
        self.path.chmod(0o640)
        actual = self.path.with_name('actual.json')
        self.path.rename(actual)
        self.path.symlink_to(actual)
        with self.assertRaises(ValueError):
            merge_allowlist(self.source.role_arn)
        self.assertEqual(json.loads(actual.read_text()), [self.old_arn])

    def test_runtime_roles_cannot_process_activation(self):
        request = self.request()
        for role in ('web', 'collector'):
            with override_settings(RUNTIME_ROLE=role), self.assertRaises(PermissionDenied):
                process_activation(request)

    def test_broker_response_must_match_exact_request(self):
        with patch('billing.activation.boto3.client') as client:
            client.return_value.invoke.return_value = {'StatusCode': 200, 'Payload': io.BytesIO(json.dumps({'status':'allowed','role_arn':self.old_arn}).encode())}
            with self.assertRaises(ValueError):
                allow_role_via_broker(self.source.role_arn)
            kwargs = client.return_value.invoke.call_args.kwargs
            self.assertEqual(json.loads(kwargs['Payload']), {'role_arn': self.source.role_arn})


@skipUnless(connection.vendor == 'postgresql', 'Requires actual PostgreSQL runtime identities')
class ActivationDatabaseRoleTests(TransactionTestCase):
    def setUp(self):
        import psycopg
        from psycopg import sql
        self.psycopg = psycopg
        with connection.cursor() as cursor:
            cursor.execute((Path(__file__).parents[2] / 'deploy/database-roles.sql').read_text())
        self.admin = User.objects.create_superuser('activation-sql-admin', password='isolated-test-password')
        self.profile = UserSecurity.objects.create(user=self.admin, portfolio_access=True)
        self.operator = User.objects.create_user('activation-sql-operator')
        self.customer, self.source = make_customer('SQL activation', '123456789012', kind='standalone', connected=False)
        self.source.role_arn = self.source.expected_role_arn
        self.source.save()
        self.consent = json.dumps({'contact':'Approved contact','evidence':'Test-only approval','confirmed':True,'retention_days':365})
        self.password = secrets.token_urlsafe(32)
        with connection.cursor() as cursor:
            for role in ('billing_web','billing_collector'):
                cursor.execute(sql.SQL('ALTER ROLE {} PASSWORD {}').format(sql.Identifier(role), sql.Literal(self.password)).as_string())

    def connect(self, role):
        cfg = connection.settings_dict
        return self.psycopg.connect(host=cfg['HOST'], port=cfg['PORT'] or 5432, dbname=cfg['NAME'], user=role, password=self.password)

    def call(self, client, identity=None, mfa=True, version=1):
        client.execute("SELECT set_config('billing.user_id',%s,true),set_config('billing.mfa_verified',%s,true)",
            [str(identity.pk) if identity else '', 'true' if mfa else 'false'])
        return client.execute('SELECT billing_request_activation(%s,%s,%s::jsonb,%s,%s)',
            [self.source.pk, version, self.consent, 'ap-south-1', 'arn:aws:iam::999999999999:role/CloudBillingCollector']).fetchone()[0]

    def test_only_live_mfa_portfolio_identity_can_submit_immutable_request(self):
        with self.connect('billing_web') as client:
            for identity, mfa, version in ((None,True,1),(self.operator,True,1),(self.admin,False,1),(self.admin,True,9)):
                with self.subTest(identity=identity, mfa=mfa, version=version), self.assertRaises(self.psycopg.errors.InsufficientPrivilege):
                    with client.transaction():
                        self.call(client, identity, mfa, version)
            identifier = self.call(client, self.admin)
            self.assertEqual(identifier, self.call(client, self.admin))
            row = client.execute('SELECT requested_by_id,session_version,snapshot FROM billing_activationrequest WHERE id=%s', [identifier]).fetchone()
            self.assertEqual(row[0], self.admin.pk)
            self.assertEqual(row[1], 1)
            self.assertEqual(row[2]['source'], source_snapshot(self.source))
            for statement in ('INSERT INTO billing_activationrequest DEFAULT VALUES',
                              "UPDATE billing_activationrequest SET status='completed'", 'DELETE FROM billing_activationrequest',
                              "UPDATE billing_customerapproval SET status='approved'", "UPDATE billing_roleapproval SET status='approved'"):
                with self.subTest(statement=statement), self.assertRaises(self.psycopg.errors.InsufficientPrivilege):
                    with client.transaction():
                        client.execute(statement)
        self.assertFalse(RoleApproval.objects.filter(status='approved').exists())

    def test_collector_cannot_submit_or_modify_activation_requests(self):
        with self.connect('billing_collector') as client:
            with self.assertRaises(self.psycopg.errors.InsufficientPrivilege):
                with client.transaction():
                    self.call(client, self.admin)
            for statement in ('INSERT INTO billing_activationrequest DEFAULT VALUES', "UPDATE billing_activationrequest SET status='completed'"):
                with self.assertRaises(self.psycopg.errors.InsufficientPrivilege):
                    with client.transaction():
                        client.execute(statement)

    def test_guarded_function_rejects_role_outside_customer_billing_path(self):
        self.source.role_arn = 'arn:aws:iam::123456789012:role/Administrator'
        self.source.save()
        with self.connect('billing_web') as client, self.assertRaises(self.psycopg.errors.RaiseException):
            self.call(client, self.admin)
