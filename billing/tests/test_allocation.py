"""Project allocation rules: disjointness, versioning, previews and reconciliation."""
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import Mock
from django.test import TestCase
from django.utils import timezone
from billing import allocation
from billing.models import AllocationRule, ExplorerQuery, Project, ProjectCost, CustomerApproval
from billing.query_cache import run_query
from .helpers import cost, make_customer


class AllocationTests(TestCase):
    def setUp(self):
        self.customer, self.source = make_customer('Alloc Co', '111111111111', accounts=('222222222222', '333333333333'))
        CustomerApproval.objects.create(customer=self.customer,status='approved',metadata=['tag:Project'])
        self.source.capabilities.update(active_tag_keys=['Project'],tags=True);self.source.save(update_fields=['capabilities'])
        self.today = timezone.now().date()
        self.month = self.today.replace(day=1)
        self.day = self.month
        cost(self.source, self.day, '10', account_id='111111111111', service='AWS Support')
        cost(self.source, self.day, '100', account_id='222222222222')
        cost(self.source, self.day, '50', account_id='333333333333')
        cost(self.source, self.day, '-5', account_id='333333333333', service='Credit')
        self.erp = Project.objects.create(customer=self.customer, name='ERP')
        self.web = Project.objects.create(customer=self.customer, name='Web')

    def rule(self, project, kind='accounts', accounts=(), key='', values=(), **extra):
        rule = AllocationRule(project=project, kind=kind, account_ids=list(accounts), key=key, values=list(values), **extra)
        allocation.validate_rule(rule, self.customer)
        rule.save()
        return rule

    def test_account_rules_allocate_exactly_and_reconcile(self):
        self.rule(self.erp, accounts=['222222222222'])
        self.rule(self.web, accounts=['333333333333'])
        allocation.allocate_project(self.erp, today=self.today)
        allocation.allocate_project(self.web, today=self.today)
        result = allocation.reconcile(self.customer, self.month, self.today)
        self.assertEqual(result['total'], Decimal('155'))
        amounts = {p['project'].name: p['amount'] for p in result['projects']}
        self.assertEqual(amounts, {'ERP': Decimal('100'), 'Web': Decimal('45')})
        self.assertEqual(result['remainder'], Decimal('10'))  # management account support charge stays shared
        self.assertTrue(result['reconciles'])

    def test_overlapping_account_rules_are_rejected(self):
        self.rule(self.erp, accounts=['222222222222'])
        with self.assertRaises(ValueError):
            self.rule(self.web, accounts=['222222222222', '333333333333'])
        with self.assertRaises(ValueError):
            self.rule(self.web, accounts=['999999999999'])  # not the customer's account

    def test_tag_rules_same_key_disjoint_values_allowed_and_overlap_rejected(self):
        self.rule(self.erp, kind='tag', key='Project', values=['ERP'])
        self.rule(self.web, kind='tag', key='Project', values=['Web'])
        with self.assertRaises(ValueError):
            self.rule(Project.objects.create(customer=self.customer, name='Dup'), kind='tag', key='Project', values=['ERP', 'Other'])
        with self.assertRaises(ValueError):
            self.rule(Project.objects.create(customer=self.customer, name='Env'), kind='cost_category', key='Environment', values=['prod'])
        # a customer-wide tag rule and an account-scoped rule with a different key are ambiguous too
        with self.assertRaises(ValueError):
            self.rule(Project.objects.create(customer=self.customer, name='Scoped'), kind='account_category', accounts=['333333333333'], key='Environment', values=['prod'])
        # account-scoped rules on disjoint accounts may use different keys
        AllocationRule.objects.filter(project__in=[self.erp, self.web]).delete()
        self.rule(self.erp, kind='account_tag', accounts=['222222222222'], key='Project', values=['ERP'])
        self.rule(self.web, kind='account_category', accounts=['333333333333'], key='Environment', values=['prod'])

    def test_tag_rule_excludes_accounts_claimed_by_account_rules(self):
        self.rule(self.erp, accounts=['222222222222'])
        tag_rule = self.rule(self.web, kind='tag', key='Project', values=['Web'])
        allocation.allocate_project(self.web, today=self.today)
        query = ExplorerQuery.objects.get(source=self.source, parameters__TimePeriod__Start=str(self.month))
        accounts = query.parameters['Filter']['And'][1]['Dimensions']['Values']
        self.assertEqual(accounts, ['111111111111', '333333333333'])
        self.assertEqual(query.parameters['Filter']['And'][0], {'Tags': {'Key': 'Project', 'Values': ['Web']}})
        self.assertEqual(query.parameters['GroupBy'], [{'Type': 'DIMENSION', 'Key': 'LINKED_ACCOUNT'}])
        # pending until the worker returns data
        result = allocation.reconcile(self.customer, self.month, self.today)
        self.assertTrue(result['pending'])
        client = Mock()
        client.get_cost_and_usage.return_value = {'ResultsByTime': [{'TimePeriod': {'Start': str(self.day), 'End': str(self.day + timedelta(days=1))}, 'Estimated': False,
                                                                     'Groups': [{'Keys': ['333333333333'], 'Metrics': {'UnblendedCost': {'Amount': '20', 'Unit': 'USD'}, 'AmortizedCost': {'Amount': '20', 'Unit': 'USD'}}}]}]}
        run_query(query, client)
        allocation.allocate_project(self.web, today=self.today)
        self.assertEqual(ProjectCost.objects.filter(project=self.web).get().unblended, Decimal('20'))
        result = allocation.reconcile(self.customer, self.month, self.today)
        self.assertFalse(result['pending'])
        self.assertEqual(result['remainder'], Decimal('155') - Decimal('20'))

    def test_rule_change_is_versioned_and_effective_dated(self):
        old = self.rule(self.erp, accounts=['222222222222'], effective_start=date(2000, 1, 1))
        allocation.allocate_project(self.erp, today=self.today)
        self.assertEqual(ProjectCost.objects.filter(project=self.erp).count(), 1)
        old.effective_end = self.month
        old.save()
        new = self.rule(self.erp, accounts=['333333333333'], effective_start=self.month, version=2)
        allocation.allocate_project(self.erp, today=self.today)
        rows = ProjectCost.objects.filter(project=self.erp)
        self.assertEqual({r.account_id for r in rows}, {'333333333333'})
        self.assertEqual(rows.get(account_id='333333333333', rule=new).unblended, Decimal('45'))

    def test_preview_account_rule(self):
        rule = AllocationRule(project=self.erp, kind='accounts', account_ids=['222222222222'])
        preview = allocation.preview_rule(rule, self.customer, self.month, self.today, today=self.today)
        self.assertFalse(preview['pending'])
        self.assertEqual(preview['totals']['USD']['unblended'], Decimal('100'))

    def test_allocation_never_exceeds_customer_spend_with_credits(self):
        self.rule(self.erp, accounts=['333333333333'])
        allocation.allocate_project(self.erp, today=self.today)
        result = allocation.reconcile(self.customer, self.month, self.today)
        self.assertEqual(result['projects'][0]['amount'], Decimal('45'))
        self.assertTrue(result['reconciles'])
