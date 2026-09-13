from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone
from billing.models import CustomerMembership, UserSecurity
from billing.tests.helpers import make_customer, assign
from billing.tests.test_security import secure_settings


@secure_settings
class MyAccessTests(TestCase):
    def setUp(self):
        self.a, self.source = make_customer('Visible customer', '123456789012')
        self.b, _ = make_customer('Hidden customer', '210987654321')
        assign('333333333333', self.a, self.source)
        self.user = User.objects.create_user('customer-reader', password='test-only-unique-password')
        CustomerMembership.objects.create(user=self.user, customer=self.a, role='viewer', account_ids=['123456789012'])
        self.client.force_login(self.user)
        s = self.client.session; s['mfa_at'] = timezone.now().timestamp(); s.save()

    def test_viewer_sees_only_assigned_accounts_and_no_management_navigation(self):
        response = self.client.get('/my-access/')
        self.assertContains(response, 'Visible customer')
        self.assertContains(response, '123456789012')
        self.assertNotContains(response, '333333333333')
        self.assertNotContains(response, 'Hidden customer')
        self.assertNotContains(response, 'Create customer login')
        self.assertNotContains(response, '>Onboarding</a>')
        self.assertEqual(self.client.post('/my-access/').status_code, 405)

    def test_administrator_gets_customer_login_preset_without_creating_user(self):
        self.user.is_staff = self.user.is_superuser = True; self.user.save()
        UserSecurity.objects.filter(user=self.user).update(portfolio_access=True)
        response = self.client.get('/users/add/', {'customer': str(self.a.pk)})
        self.assertEqual(response.status_code, 200)
        form = response.context['form']
        self.assertEqual(form.initial['role'], 'viewer')
        self.assertEqual(form.initial['customers'], [self.a.pk])
        self.assertEqual(User.objects.count(), 1)

    def test_no_customer_grants_does_not_show_any_customer(self):
        CustomerMembership.objects.filter(user=self.user).update(active=False)
        response = self.client.get('/my-access/')
        self.assertContains(response, 'No customer access assigned')
        self.assertNotContains(response, 'Visible customer')
