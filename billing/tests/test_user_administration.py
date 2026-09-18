"""User-facing identity management and a login boundary that requires both factors."""
import time
from datetime import timedelta
from unittest.mock import patch
from django.contrib.auth.models import User
from django.contrib.auth.hashers import make_password
from django.test import TestCase, Client
from django.utils import timezone
from django_otp.oath import totp
from django_otp.plugins.otp_totp.models import TOTPDevice
from billing.models import UserSecurity, CustomerMembership, AuditEvent, Cost, Customer
from billing.tests.helpers import make_customer, cost
from billing.tests.test_security import secure_settings


@secure_settings
class SignInTests(TestCase):
    def setUp(self):
        self.password='unique-login-test-password'
        self.user=User.objects.create_user('sign-in-user',password=self.password)
        self.profile=UserSecurity.objects.create(user=self.user)
        self.device=TOTPDevice.objects.create(user=self.user,confirmed=True)
    def code(self):
        return str(totp(self.device.bin_key,step=self.device.step,t0=self.device.t0,digits=self.device.digits))
    def primary(self,**extra):
        return self.client.post('/login/',dict(username=self.user.username,password=self.password,**extra))
    def test_combined_password_and_mfa_never_renders_challenge_on_dashboard(self):
        page=self.client.get('/login/')
        self.assertContains(page,'Authenticator or recovery code')
        self.assertNotContains(page,'id="sidebar"')
        self.assertRedirects(self.primary(token=self.code()),'/',fetch_redirect_response=False)
        self.assertIn('mfa_at',self.client.session)
        self.assertContains(self.client.get('/'),'Hide sidebar')
    def test_password_only_and_invalid_mfa_do_not_create_authenticated_session(self):
        self.assertContains(self.primary(),'Verify your sign-in')
        self.assertNotIn('_auth_user_id',self.client.session)
        self.assertNotContains(self.client.get('/login/'),'id="sidebar"')
        self.assertEqual(self.client.get('/customers/').status_code,302)
        self.assertContains(self.client.post('/login/',{'token':'not-valid'}),'Code invalid')
        self.assertNotIn('_auth_user_id',self.client.session)
    def test_pending_login_expires_and_is_bound_to_password_and_access_version(self):
        for invalidate in ('expiry','version','password'):
            with self.subTest(invalidate=invalidate):
                self.client=Client();self.primary()
                if invalidate=='expiry':
                    session=self.client.session;session['pending_login']['at']=time.time()-301;session.save()
                elif invalidate=='version':
                    self.profile.session_version+=1;self.profile.save()
                else:
                    self.user.set_password('changed-test-password');self.user.save()
                response=self.client.post('/login/',{'token':self.code()})
                self.assertNotIn('_auth_user_id',self.client.session)
                self.assertNotContains(response,'id="sidebar"')
    def test_replay_recovery_and_safe_redirect(self):
        # Replay the exact accepted token, even if a new TOTP time step begins.
        used_code = self.code()
        self.assertRedirects(self.primary(token=used_code,next='https://example.invalid/'),'/',fetch_redirect_response=False)
        self.client.logout();self.primary(token=used_code)
        self.assertNotIn('_auth_user_id',self.client.session)
        recovery='synthetic-recovery-code-123456789'
        self.profile.refresh_from_db();self.profile.recovery_hashes=[make_password(recovery)];self.profile.recovery_locked_until=None;self.profile.save()
        self.assertEqual(self.client.post('/login/',{'token':recovery}).status_code,302)
        self.profile.refresh_from_db();self.assertEqual(self.profile.recovery_hashes,[])
    def test_legacy_password_only_session_is_replaced_with_a_challenge(self):
        self.client.force_login(self.user)
        self.assertRedirects(self.client.get('/customers/'),'/login/',fetch_redirect_response=False)
        self.assertNotIn('_auth_user_id',self.client.session)
        self.assertIn('pending_login',self.client.session)


@secure_settings
class UserAdministrationTests(TestCase):
    def setUp(self):
        self.admin=User.objects.create_superuser('admin',password='admin-test-only-password')
        UserSecurity.objects.create(user=self.admin,portfolio_access=True)
        self.a,self.sa=make_customer('Access Alpha','123456789012')
        self.b,self.sb=make_customer('Access Beta','210987654321')
        cost(self.sa,timezone.now().date(),10)
        self.login(self.admin)
    def login(self,user):
        self.client.force_login(user)
        session=self.client.session;session['mfa_at']=time.time();session.save()
    def payload(self,**changes):
        data={'username':'managed-user','first_name':'Managed','last_name':'User','email':'managed@example.invalid','role':'viewer','active':'on','customers':[str(self.a.pk)],'account_ids':'','password1':'initial-test-only-password','password2':'initial-test-only-password'}
        data.update(changes);return data
    def test_create_read_scope_edit_revoke_and_delete_preserve_billing_audit(self):
        self.assertContains(self.client.get('/users/'),'Users &amp; access')
        self.assertRedirects(self.client.post('/users/add/',self.payload()),'/users/',fetch_redirect_response=False)
        user=User.objects.get(username='managed-user')
        self.assertTrue(user.check_password('initial-test-only-password'))
        self.assertFalse(user.is_superuser)
        self.assertEqual(CustomerMembership.objects.get(user=user).customer_id,self.a.pk)
        self.assertFalse(TOTPDevice.objects.filter(user=user).exists())
        self.assertContains(self.client.get(f'/users/{user.pk}/'),'Limit to AWS account IDs')
        scoped=Client();scoped.force_login(user);session=scoped.session;session['mfa_at']=time.time();session.save()
        self.assertContains(scoped.get('/customers/'),'Access Alpha')
        self.assertNotContains(scoped.get('/customers/'),'Access Beta')
        self.assertEqual(scoped.get('/users/').status_code,403)
        self.assertEqual(scoped.post('/users/add/',self.payload(username='intruder')).status_code,403)
        self.assertRedirects(self.client.post(f'/users/{user.pk}/',self.payload(customers=[str(self.b.pk)],password1='',password2='')),'/users/',fetch_redirect_response=False)
        self.assertEqual(scoped.get('/customers/').status_code,302)
        self.assertFalse(CustomerMembership.objects.get(user=user,customer=self.a).active)
        before=Cost.objects.count()
        self.assertRedirects(self.client.post(f'/users/{user.pk}/',{'action':'delete','confirm_username':user.username}),'/users/',fetch_redirect_response=False)
        self.assertFalse(User.objects.filter(pk=user.pk).exists())
        self.assertEqual(Cost.objects.count(),before)
        self.assertTrue(AuditEvent.objects.filter(action='User delete').exists())
    def test_admin_has_future_customers_and_cannot_remove_self(self):
        self.assertContains(self.client.get('/customers/'),'Access Beta')
        Customer.objects.create(name='Future customer')
        self.assertContains(self.client.get('/customers/'),'Future customer')
        response=self.client.post(f'/users/{self.admin.pk}/',{'action':'delete','confirm_username':'admin'})
        self.assertContains(response,'deletion was refused')
        self.assertTrue(User.objects.filter(pk=self.admin.pk).exists())
        response=self.client.post(f'/users/{self.admin.pk}/',self.payload(username='admin',active='',role='viewer',password1='',password2=''))
        self.assertContains(response,'cannot remove your own administrator access')
    def test_invalid_customer_account_grants_and_passwords_rejected(self):
        for values in ({'account_ids':self.sb.account_id},{'password1':'weak','password2':'weak'},{'customers':[],'account_ids':self.sa.account_id},{'role':'admin'}):
            with self.subTest(values=values):
                self.assertEqual(self.client.post('/users/add/',self.payload(**values)).status_code,200)
                self.assertFalse(User.objects.filter(username='managed-user').exists())
    def test_ungranted_superuser_mfa_and_csrf_cannot_bypass_admin_gate(self):
        other=User.objects.create_superuser('ungranted');UserSecurity.objects.create(user=other)
        self.login(other);self.assertEqual(self.client.get('/users/').status_code,403)
        self.client.force_login(self.admin);self.assertEqual(self.client.get('/users/').status_code,302)
        client=Client(enforce_csrf_checks=True);client.force_login(self.admin);session=client.session;session['mfa_at']=time.time();session.save()
        self.assertEqual(client.post('/users/add/',self.payload()).status_code,403)
