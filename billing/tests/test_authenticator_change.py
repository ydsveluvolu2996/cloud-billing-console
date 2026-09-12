from django.contrib.auth.models import User
from django.contrib.auth.hashers import check_password, make_password
from django.test import TestCase, Client
from django.utils import timezone
from django_otp.oath import totp
from django_otp.plugins.otp_totp.models import TOTPDevice
from billing.models import UserSecurity
from billing.tests.test_security import secure_settings


@secure_settings
class AuthenticatorChangeTests(TestCase):
    url = '/security/authenticator/'

    def setUp(self):
        self.password = 'test-only-authenticator-password'
        self.user = User.objects.create_user('factor-owner', password=self.password)
        self.profile = UserSecurity.objects.create(user=self.user, recovery_hashes=[make_password('old-recovery-code')])
        self.old = TOTPDevice.objects.create(user=self.user, name='primary', confirmed=True)
        self.authenticate(self.client)

    def authenticate(self, client):
        client.force_login(self.user)
        s = client.session
        s['mfa_at'] = timezone.now().timestamp()
        s.save()

    def code(self, device):
        return str(totp(device.bin_key, step=device.step, t0=device.t0, digits=device.digits)).zfill(6)

    def start(self):
        return self.client.post(self.url, {'password': self.password, 'token': self.code(self.old)})

    def test_atomic_replacement_and_session_and_recovery_revocation(self):
        other = Client(); self.authenticate(other)
        initial_version = self.profile.session_version
        self.assertContains(self.start(), 'New authenticator setup key')
        self.old.refresh_from_db(); self.assertTrue(self.old.confirmed)
        new = TOTPDevice.objects.get(user=self.user, confirmed=False)
        response = self.client.post(self.url, {'token': self.code(new)})
        self.assertContains(response, 'Authenticator changed')
        self.assertFalse(TOTPDevice.objects.filter(pk=self.old.pk).exists())
        new.refresh_from_db(); self.assertTrue(new.confirmed)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.session_version, initial_version + 1)
        self.assertEqual(len(self.profile.recovery_hashes), 8)
        self.assertTrue(check_password(response.context['recovery_codes'][0], self.profile.recovery_hashes[0]))
        self.assertEqual(self.client.session['security_version'], initial_version + 1)
        self.assertEqual(other.get(self.url).status_code, 302)
        self.assertNotContains(self.client.get(self.url), response.context['recovery_codes'][0])
        self.assertIn('no-store', response['Cache-Control'])

    def test_wrong_password_does_not_stage_and_is_throttled(self):
        self.client.post(self.url, {'password': 'wrong', 'token': self.code(self.old)})
        self.assertFalse(TOTPDevice.objects.filter(confirmed=False).exists())
        self.assertNotContains(self.start(), 'New authenticator setup key')
        self.profile.refresh_from_db(); self.assertIsNotNone(self.profile.recovery_locked_until)

    def test_invalid_new_code_preserves_old_and_cancel_discards_pending(self):
        self.start()
        new = TOTPDevice.objects.get(confirmed=False)
        invalid = str((int(self.code(new)) + 500000) % 1000000).zfill(6)
        self.assertContains(self.client.post(self.url, {'token': invalid}), 'invalid')
        self.assertTrue(TOTPDevice.objects.filter(pk=self.old.pk, confirmed=True).exists())
        self.client.post(self.url, {'action': 'cancel'})
        self.assertFalse(TOTPDevice.objects.filter(pk=new.pk).exists())
        self.assertNotIn('authenticator_change', self.client.session)

    def test_expired_setup_cannot_activate(self):
        self.start(); new = TOTPDevice.objects.get(confirmed=False)
        s = self.client.session; pending = s['authenticator_change']; pending['at'] -= 301; s['authenticator_change'] = pending; s.save()
        self.client.post(self.url, {'token': self.code(new)})
        new.refresh_from_db(); self.assertFalse(new.confirmed)
        self.assertTrue(TOTPDevice.objects.filter(pk=self.old.pk, confirmed=True).exists())
        self.assertNotIn('authenticator_change', self.client.session)

    def test_setup_is_session_bound_and_csrf_required(self):
        self.start(); new = TOTPDevice.objects.get(confirmed=False)
        other = Client(); self.authenticate(other)
        other.post(self.url, {'token': self.code(new)})
        new.refresh_from_db(); self.assertFalse(new.confirmed)
        csrf = Client(enforce_csrf_checks=True); self.authenticate(csrf)
        self.assertEqual(csrf.post(self.url, {'password': self.password, 'token': self.code(self.old)}).status_code, 403)
        anon = Client(); self.assertEqual(anon.get(self.url).status_code, 302)
