"""Negative security tests run with real enforcement enabled, unlike legacy UI fixtures."""
from datetime import date, timedelta
from unittest.mock import Mock, patch
from tempfile import TemporaryDirectory
from pathlib import Path
import json
from django.test import TestCase, override_settings, Client
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone
from django_otp.oath import totp
from django_otp.plugins.otp_totp.models import TOTPDevice
from botocore.exceptions import ClientError, EndpointConnectionError
from billing.models import *
from billing.access import context, for_user
from billing.iam import validate_role_arn, policy_bundle, verify_trust, assert_role_allowed
from billing.aws import Meter, Session
from billing.tests.helpers import make_customer, cost, assign, FakeSession, TEST_STORAGES

secure_settings = override_settings(SECURE_SSL_REDIRECT=False, STORAGES=TEST_STORAGES, ALLOWED_HOSTS=['testserver','localhost'],
    ENFORCE_CUSTOMER_AUTHORIZATION=True, MFA_REQUIRED=True, REQUIRE_CONNECTION_APPROVAL=True,
    COLLECTOR_ROLE_ARN='arn:aws:iam::111111111111:role/Collector')


@secure_settings
class IsolationTests(TestCase):
    def setUp(self):
        self.a,self.sa=make_customer('Tenant Alpha','123456789012')
        self.b,self.sb=make_customer('Tenant Private Beta','210987654321')
        cost(self.sa,date.today(),10);cost(self.sb,date.today(),987654)
        self.user=User.objects.create_user('alice',password='test-only-unique-password')
        self.member=CustomerMembership.objects.create(user=self.user,customer=self.a,role='operator')
        self.client.force_login(self.user)
        session=self.client.session;session['mfa_at']=timezone.now().timestamp();session.save()
        self.budget=Budget.objects.create(customer=self.b,name='Private budget')
        self.project=Project.objects.create(customer=self.b,name='Private project')
        self.saved=SavedReport.objects.create(customer=self.b,name='Private saved',parameters={'customer':str(self.b.pk)},created_by='bob')
    def test_cross_customer_urls_and_exports(self):
        for url in [f'/customers/{self.b.pk}/',f'/sources/{self.sb.pk}/',f'/budgets/{self.budget.pk}/',f'/projects/{self.project.pk}/',
                    f'/reports/{self.saved.pk}/',f'/accounts/{self.sb.account_id}/',f'/alliance/{self.b.pk}/{self.sb.account_id}/']:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code,404)
        for url in ['/export/','/export/report/','/portfolio/','/explorer/metadata/?dimension=account&']:
            response=self.client.get(url+('?' if '?' not in url else '')+'customer='+str(self.b.pk))
            self.assertIn(response.status_code,(400,403,404))
            self.assertNotIn(b'987654',response.content)
    def test_lists_search_and_dashboard_do_not_reveal_other_customer(self):
        for url in ['/customers/','/overview/','/portfolio/','/budgets/','/alliance/','/onboarding/','/activity/','/customers/?q=Private','/operations/']:
            response=self.client.get(url)
            with self.subTest(url=url):
                self.assertEqual(response.status_code,200)
                self.assertNotIn(b'Tenant Private Beta',response.content)
                self.assertNotIn(b'987654',response.content)
    def test_cross_customer_mutations(self):
        for url in [f'/customers/{self.b.pk}/offboard/',f'/customers/{self.b.pk}/sync/',f'/sources/{self.sb.pk}/pause/',f'/budgets/{self.budget.pk}/']:
            self.assertIn(self.client.post(url,{'action':'deactivate'}).status_code,(403,404))
        self.sb.refresh_from_db();self.assertTrue(self.sb.enabled)
    def test_membership_revocation_invalidates_session(self):
        self.member.active=False;self.member.save()
        self.assertRedirects(self.client.get(f'/customers/{self.a.pk}/'),'/login/',fetch_redirect_response=False)
    def test_viewer_cannot_write_even_if_staff(self):
        self.member.role='viewer';self.member.save()
        self.user.is_staff=True;self.user.save()
        self.client.force_login(self.user);s=self.client.session;s['mfa_at']=timezone.now().timestamp();s.save()
        self.assertEqual(self.client.post(f'/sources/{self.sa.pk}/pause/').status_code,403)
    def test_superuser_requires_explicit_portfolio_grant(self):
        other=User.objects.create_superuser('ungranted',password='test-only-unique-password')
        with context(for_user(other)):
            self.assertEqual(Customer.objects.count(),0)
        UserSecurity.objects.update_or_create(user=other,defaults={'portfolio_access':True})
        with context(for_user(other)):
            self.assertEqual(Customer.objects.count(),2)
    def test_scope_changing_create_and_update_denied(self):
        with context(for_user(self.user,write=True)):
            with self.assertRaises(PermissionDenied):
                Budget.objects.create(customer=self.b,name='Bad scope')
            with self.assertRaises(PermissionDenied):
                Cost.objects.update(customer_id=self.b.pk)
            with self.assertRaises(PermissionDenied):
                Project.objects.bulk_create([Project(customer=self.b,name='Bad bulk')])
    def test_cache_ids_are_customer_scoped(self):
        q=ExplorerQuery.objects.create(customer=self.b,source=self.sb,fingerprint='x',connection_fingerprint='x',operation='get_cost_and_usage',parameters={},data={'secret':'private'})
        response=self.client.get('/explorer/status/?ids='+str(q.pk))
        self.assertEqual(response.json()['count'],0)
    def test_no_membership_denies_data(self):
        u=User.objects.create_user('no-membership')
        with context(for_user(u)):
            for model in (Customer,Cost,BillingSource,Budget,Project,AllianceRecord,ExplorerQuery,AwsAccount):
                self.assertEqual(model.objects.count(),0)
    def test_account_restriction_and_historical_owner(self):
        assign('333333333333',self.a,self.sa)
        cost(self.sa,date.today(),50,account_id='333333333333')
        self.member.account_ids=[self.sa.account_id];self.member.save()
        with context(for_user(self.user)):
            self.assertEqual(list(Cost.objects.values_list('account_id',flat=True)),[self.sa.account_id])
            self.assertEqual(AwsAccount.objects.count(),1)
            self.assertEqual(ExplorerQuery.objects.count(),0)
    def test_external_portal_defaults_off(self):
        UserSecurity.objects.filter(user=self.user).update(external=True)
        self.assertEqual(self.client.get('/').status_code,403)
    def test_login_requires_mfa_before_customer_access(self):
        self.client.force_login(self.user)
        self.assertRedirects(self.client.get('/customers/'),'/mfa/',fetch_redirect_response=False)
    def test_csrf_still_required(self):
        c=Client(enforce_csrf_checks=True);c.force_login(self.user)
        session=c.session;session['mfa_at']=timezone.now().timestamp();session.save()
        self.assertEqual(c.post('/sessions/revoke/').status_code,403)


@secure_settings
class IAMTests(TestCase):
    def setUp(self):
        self.customer,self.source=make_customer('Manual role','123456789012')
        self.source.role_arn='arn:aws:iam::123456789012:role/approved/path/FinanceReader'
        self.source.save()
    def test_approved_role_names_paths_accounts_partitions(self):
        self.source.full_clean()
        for value in ['arn:aws:iam::999999999999:role/Reader','arn:aws:iam::123456789012:root','arn:aws:iam::*:role/Reader','arn:aws-cn:iam::123456789012:role/Reader']:
            with self.assertRaises(ValidationError): validate_role_arn(value,self.source.account_id)
    def test_web_cannot_create_customer_aws_session(self):
        with patch('billing.aws.boto3.client') as client:
            with self.assertRaisesRegex(ValueError,'separate collector'):Session(self.source)
            client.assert_not_called()
    def trust_session(self,policy=None):
        sts=Mock();sts.get_caller_identity.return_value={'Account':self.source.account_id,'Arn':'arn:aws:sts::123456789012:assumed-role/FinanceReader/session'}
        iam=Mock();iam.get_role.return_value={'Role':{'Arn':self.source.role_arn,'AssumeRolePolicyDocument':policy or policy_bundle(self.source)['trust_policy']}}
        return FakeSession(sts=sts,iam=iam)
    def test_positive_and_explicit_negative_trust_probes(self):
        sts=Mock();sts.assume_role.side_effect=ClientError({'Error':{'Code':'AccessDenied'}},'AssumeRole')
        result=verify_trust(self.source,self.trust_session(),sts,Meter(limit=20))
        self.assertEqual(result['missing_external_id'],'denied');self.assertEqual(sts.assume_role.call_count,2)
        self.assertNotIn('ExternalId',sts.assume_role.call_args_list[0].kwargs)
        self.assertNotEqual(sts.assume_role.call_args_list[1].kwargs['ExternalId'],self.source.external_id)
    def test_timeout_is_not_denial_and_broad_principal_rejected(self):
        sts=Mock();sts.assume_role.side_effect=EndpointConnectionError(endpoint_url='https://sts.example.invalid')
        with self.assertRaises(EndpointConnectionError):verify_trust(self.source,self.trust_session(),sts,Meter())
        trust=policy_bundle(self.source)['trust_policy'];trust['Statement'][0]['Principal']={'AWS':'*'}
        with self.assertRaisesRegex(ValueError,'Trust must'):verify_trust(self.source,self.trust_session(trust),Mock(),Meter())
    def test_wrong_external_id_success_and_unrelated_error_rejected(self):
        with self.assertRaisesRegex(ValueError,'succeeded'):verify_trust(self.source,self.trust_session(),Mock(),Meter())
        sts=Mock();sts.assume_role.side_effect=ClientError({'Error':{'Code':'Throttling'}},'AssumeRole')
        with override_settings(AWS_THROTTLE_RETRIES=0):
            with self.assertRaisesRegex(ValueError,'inconclusive'):verify_trust(self.source,self.trust_session(),sts,Meter())
    def test_allowlist_requires_customer_approval_and_exact_deployed_arn(self):
        with TemporaryDirectory() as directory, override_settings(RUNTIME_ROLE='collector',COLLECTOR_ALLOWLIST_FILE=directory+'/roles.json'):
            Path(directory+'/roles.json').write_text(json.dumps([self.source.role_arn]))
            with self.assertRaisesRegex(ValueError,'approval'):assert_role_allowed(self.source)
            CustomerApproval.objects.create(customer=self.customer,contacts=['synthetic reviewer'],authorized_users=['synthetic'],expected_accounts=[self.source.account_id],billing_fields=['cost'],storage_region='ap-south-1',retention_days=30,status='approved',evidence='synthetic-test',approved_by='test',approved_at=timezone.now())
            RoleApproval.objects.create(source=self.source,role_arn=self.source.role_arn,connection_version=1,status='approved',evidence='synthetic-test',requested_by='test',approved_by='test',approved_at=timezone.now())
            assert_role_allowed(self.source)
            Path(directory+'/roles.json').write_text('[]')
            with self.assertRaisesRegex(ValueError,'deployed'):assert_role_allowed(self.source)


@secure_settings
class MFATests(TestCase):
    def setUp(self):
        self.user=User.objects.create_user('individual',password='test-only-unique-password')
        self.client.force_login(self.user)
    def test_enrollment_replay_recovery_and_revocation(self):
        self.assertContains(self.client.get('/mfa/'),'Set up two-step verification')
        device=TOTPDevice.objects.get(user=self.user)
        token=str(totp(device.bin_key,step=device.step,t0=device.t0,digits=device.digits))
        response=self.client.post('/mfa/',{'token':token})
        self.assertContains(response,'Save your recovery codes')
        device.refresh_from_db();self.assertTrue(device.confirmed)
        profile=UserSecurity.objects.get(user=self.user);self.assertEqual(len(profile.recovery_hashes),8)
        code=response.context['recovery_codes'][0]
        self.assertNotIn(code,profile.recovery_hashes)
        self.client.force_login(self.user)
        self.assertContains(self.client.post('/mfa/',{'token':token}),'Code invalid')
        UserSecurity.objects.filter(user=self.user).update(recovery_locked_until=None)
        self.assertRedirects(self.client.post('/mfa/',{'token':code}),'/',fetch_redirect_response=False)
        profile.refresh_from_db();self.assertEqual(len(profile.recovery_hashes),7)
        self.assertRedirects(self.client.post('/sessions/revoke/'),'/login/',fetch_redirect_response=False)
        self.assertRedirects(self.client.get('/customers/'),'/login/?next=/customers/',fetch_redirect_response=False)
    def test_recovery_cannot_enroll_over_confirmed_device(self):
        TOTPDevice.objects.create(user=self.user,confirmed=True,name='existing')
        self.assertNotContains(self.client.get('/mfa/'),'otpauth://')

@secure_settings
class ScopeChangeTests(TestCase):
    def setUp(self):
        self.a,self.source=make_customer('Shared custodian','123456789012',shared=True)
        self.b,_=make_customer('Consumer','222222222222')
        assign('333333333333',self.b,self.source)
        cost(self.source,date.today(),10,account_id='333333333333')
        self.user=User.objects.create_user('scoped-reader')
        self.membership=CustomerMembership.objects.create(user=self.user,customer=self.b,role='viewer')
    def test_shared_report_job_and_permission_change_invalidate_cache(self):
        from billing.query_cache import get_query,run_query
        from billing.scheduler import load_source
        with context(for_user(self.user)):
            q=get_query(self.source,'get_cost_and_usage',{'TimePeriod':{'Start':'2026-01-01','End':'2026-02-01'}},customer=self.b,account_filter=['333333333333'])
        with override_settings(REQUIRE_CONNECTION_APPROVAL=False):
            self.assertEqual(load_source(Job.objects.get(kind='explorer_refresh')).pk,self.source.pk)
        self.membership.account_ids=['222222222222'];self.membership.save()
        client=Mock()
        run_query(q,client)
        client.get_cost_and_usage.assert_not_called()
        with context(for_user(self.user)):self.assertFalse(ExplorerQuery.objects.filter(pk=q.pk).exists())
    def test_ownership_transfer_invalidates_cached_fingerprint(self):
        from billing.query_cache import connection_key
        from billing.collector import ensure_assignment
        before=connection_key(self.source)
        account=AwsAccount.objects.get(account_id='333333333333')
        ensure_assignment(account,self.a,start=date.today(),actor='synthetic')
        self.assertNotEqual(connection_key(self.source),before)
    def test_expired_support_membership_denies_every_model(self):
        self.membership.expires_at=timezone.now()-timedelta(seconds=1);self.membership.save()
        with context(for_user(self.user)):
            self.assertEqual(Cost.objects.count(),0);self.assertEqual(Customer.objects.count(),0)
    def test_sensitive_audit_details_are_omitted(self):
        from billing.authentication import security_event
        security_event('test','Redaction regression',customer=self.b,password='never-store',snapshot={'amount':'sensitive'},access_token='never-store')
        encoded=json.dumps(AuditEvent.objects.get(action='Redaction regression').details)
        self.assertNotIn('never-store',encoded);self.assertNotIn('sensitive',encoded)


@secure_settings
class OIDCTests(TestCase):
    def test_signed_nonce_expiry_issuer_audience_and_exact_subject(self):
        import jwt
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization
        from billing.oidc import BillingOIDCBackend
        from django.core.exceptions import SuspiciousOperation
        key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        private=key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption())
        public=key.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo)
        now=int(timezone.now().timestamp())
        payload={'iss':'https://identity.example.invalid','aud':'synthetic-client','sub':'subject-1','iat':now,'exp':now+300,'nonce':'synthetic-nonce'}
        with override_settings(OIDC_RP_CLIENT_ID='synthetic-client',OIDC_RP_IDP_SIGN_KEY=public,OIDC_ISSUER=payload['iss']):
            backend=BillingOIDCBackend()
            token=jwt.encode(payload,private,algorithm='RS256')
            self.assertEqual(backend.verify_token(token,nonce='synthetic-nonce')['sub'],'subject-1')
            for changes in ({'iss':'https://foreign.example.invalid'},{'aud':'foreign-client'},{'nonce':'wrong'},{'exp':now-1},{'iat':now+600}):
                with self.subTest(changes=changes),self.assertRaises((SuspiciousOperation,jwt.InvalidTokenError)):
                    backend.verify_token(jwt.encode({**payload,**changes},private,algorithm='RS256'),nonce='synthetic-nonce')
            self.assertIsNone(backend.get_or_create_user(None,None,payload))
            user=User.objects.create_user('provisioned',email='same@example.invalid')
            UserSecurity.objects.create(user=user,oidc_issuer=payload['iss'],oidc_subject=payload['sub'])
            self.assertEqual(backend.get_or_create_user(None,None,payload),user)
            self.assertIsNone(backend.get_or_create_user(None,None,{**payload,'sub':'different','email':user.email}))

@secure_settings
class PublicationTests(TestCase):
    def test_billing_discovery_does_not_assign_unapproved_accounts(self):
        from billing.collector import owner_lookup
        c,s=make_customer('Inventory approval','123456789012')
        CustomerApproval.objects.create(customer=c,status='approved',expected_accounts=[s.account_id])
        rows=[{'account_id':'555555555555','day':date.today()}]
        result=owner_lookup(s,rows)
        self.assertIsNone(result[('555555555555',date.today())])
        self.assertFalse(AccountAssignment.objects.filter(account__account_id='555555555555').exists())
    def test_cancelled_lease_cannot_publish_or_renew_replacement(self):
        from billing import jobs
        from billing.collector import publish_month,ConnectionChanged
        c,s=make_customer('Lease safety','123456789012')
        job,_=jobs.enqueue('collect',source=s);old=jobs.lease('worker-old')
        Job.objects.filter(pk=old.pk).update(status=Job.QUEUED)
        new=jobs.lease('worker-new')
        previous=new.lease_expires
        jobs.heartbeat(old,{'bad':'old-worker'})
        with self.assertRaises(ConnectionChanged):publish_month(s,date.today().replace(day=1),[],[],True,Meter(),timezone.now(),job=old)
        new.refresh_from_db();self.assertEqual(new.progress,{})
        self.assertEqual(new.lease_expires,previous)
