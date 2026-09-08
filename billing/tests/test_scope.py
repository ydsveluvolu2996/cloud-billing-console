"""Centralized scope authorization: manipulated IDs never expose another customer's data."""
from datetime import date
from decimal import Decimal
from unittest.mock import Mock
from django.contrib.auth.models import User
from django.test import TestCase, Client
from django.utils import timezone
from billing import scope as scoping
from billing.advanced_explorer import build_report
from billing.models import ExplorerQuery, Project, SavedReport
from billing.query_cache import run_query
from billing.reporting import report
from .helpers import assign, cost, make_customer, web_settings


@web_settings
class ScopeTests(TestCase):
    def setUp(self):
        self.a, self.a_source = make_customer('Alpha', '111111111111', accounts=('111111111112',))
        self.b, self.b_source = make_customer('Beta', '222222222222', accounts=('222222222223',))
        self.day = date(2026, 9, 1)
        cost(self.a_source, self.day, '10', account_id='111111111112')
        cost(self.b_source, self.day, '20', account_id='222222222223')
        self.reader = User.objects.create_user('reader', password='test-only-a-long-password')
        self.client = Client()
        self.client.force_login(self.reader)

    def test_account_from_another_customer_is_rejected(self):
        with self.assertRaises(ValueError):
            report({'start': '2026-09-01', 'end': '2026-09-01', 'customer': str(self.a.pk), 'account': '222222222223'})
        response = self.client.get(f'/portfolio/?start=2026-09-01&end=2026-09-01&customer={self.a.pk}&account=222222222223')
        self.assertEqual(response.status_code, 400)
        response = self.client.get(f'/export/?start=2026-09-01&end=2026-09-01&customer={self.a.pk}&account=222222222223')
        self.assertEqual(response.status_code, 400)
        response = self.client.get(f'/accounts/222222222223/?customer={self.a.pk}')
        self.assertEqual(response.status_code, 400)
        response = self.client.get(f'/?customer={self.a.pk}&account=222222222223&start=2026-09-01&end=2026-09-01')
        self.assertEqual(response.status_code, 400)

    def test_source_and_project_must_belong_to_customer(self):
        with self.assertRaises(ValueError):
            scoping.resolve({'customer': str(self.a.pk), 'source': str(self.b_source.pk)})
        project = Project.objects.create(customer=self.b, name='P')
        with self.assertRaises(ValueError):
            scoping.resolve({'customer': str(self.a.pk), 'project': str(project.pk)})
        self.assertEqual(scoping.resolve({'project': str(project.pk)}).customer, self.b)

    def test_customer_scope_excludes_other_costs_and_unassigned(self):
        result = report({'start': '2026-09-01', 'end': '2026-09-01', 'customer': str(self.a.pk)})
        self.assertEqual(result['total'], Decimal('10'))
        self.assertEqual([r['account_id'] for r in result['account_rows']], ['111111111112'])

    def test_shared_payer_queries_carry_account_filter(self):
        owner, shared = make_customer('Shared owner', '333333333333', shared=True)
        tenant, _ = make_customer('Tenant', '444444444444', connected=False)
        assign('333333333334', tenant, shared)
        assign('333333333335', owner, shared)
        units = scoping.report_units(tenant)
        self.assertEqual([(u[0].pk, u[2]) for u in units], [(shared.pk, ['333333333334'])])
        self.assertIsNone(scoping.source_account_filter(self.a_source, self.a))
        params = {'start': '2026-09-01', 'end': '2026-09-01', 'group_by': 'region', 'forecast': '0', 'customer': str(tenant.pk)}
        build_report(params)
        query = ExplorerQuery.objects.get(customer=tenant)
        self.assertEqual(query.parameters['Filter'], {'Dimensions': {'Key': 'LINKED_ACCOUNT', 'Values': ['333333333334']}})
        # metadata for the tenant never lists the owner's accounts
        query = ExplorerQuery.objects.filter(operation='get_dimension_values')
        response = self.client.get(f'/explorer/metadata/?dimension=account&start=2026-09-01&end=2026-09-01&customer={tenant.pk}')
        q = ExplorerQuery.objects.get(operation='get_dimension_values', customer=tenant)
        client = Mock()
        client.get_dimension_values.return_value = {'DimensionValues': [{'Value': '333333333334'}, {'Value': '333333333335'}]}
        run_query(q, client)
        data = self.client.get(f'/explorer/metadata/?dimension=account&start=2026-09-01&end=2026-09-01&customer={tenant.pk}').json()
        self.assertEqual(data['values'], ['333333333334'])

    def test_saved_report_with_foreign_account_is_rejected(self):
        from billing.parameters import querydict, normalize
        staff = User.objects.create_user('staff', password='test-only-a-long-password', is_staff=True)
        client = Client()
        client.force_login(staff)
        p = normalize({'start': '2026-09-01', 'end': '2026-09-01', 'customer': str(self.a.pk), 'account': ['222222222223']})
        response = client.post('/reports/save/', querydict(p))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(SavedReport.objects.count(), 0)

    def test_readers_cannot_edit_but_operators_can(self):
        self.assertEqual(self.client.post(f'/accounts/111111111112/assign/', {'customer': self.b.pk, 'start': '2026-09-01'}).status_code, 403)
        self.assertEqual(self.client.post(f'/budgets/1/', {'action': 'deactivate'}).status_code in (403, 404), True)
        self.assertEqual(self.client.get('/onboarding/').status_code, 200)
        self.assertEqual(self.client.get('/budgets/').status_code, 200)
        self.assertEqual(self.client.get(f'/customers/{self.a.pk}/projects/add/').status_code, 403)
        self.assertEqual(scoping.role(self.reader), 'reader')
        staff = User.objects.create_user('staff', password='test-only-a-long-password', is_staff=True)
        self.assertEqual(scoping.role(staff), 'operator')
        client = Client()
        client.force_login(staff)
        response = client.post(f'/accounts/111111111112/assign/', {'customer': self.b.pk, 'start': '2026-09-01'})
        self.assertEqual(response.status_code, 302)
        from billing.models import Cost
        self.assertEqual(Cost.objects.get(account_id='111111111112').customer, self.b)  # re-stamped from the start date
        cost(self.a_source, date(2026, 8, 15), '3', account_id='111111111112')
        self.assertEqual(Cost.objects.get(day=date(2026, 8, 15)).customer, self.a)
