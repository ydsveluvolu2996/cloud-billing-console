from unittest.mock import patch

from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from billing import scheduler
from billing.iam import approval_ready
from billing.management.commands.security_load import Command as SecurityLoad
from billing.management.commands.synthetic_load import Command as SyntheticLoad
from billing.models import BillingSource, Customer, CustomerApproval, RoleApproval
from billing.tests.helpers import make_customer


@override_settings(DEBUG=True, REQUIRE_CONNECTION_APPROVAL=True, AWS_REGION='ap-south-1')
class SyntheticApprovalTests(TestCase):
    def test_heterogeneous_fixture_collects_with_real_approval_checks_and_no_aws(self):
        command = SecurityLoad()
        options = {'customers': 2, 'accounts': 4, 'months': 1, 'services': 1, 'collect_sources': 10}
        with patch('boto3.client', side_effect=AssertionError('Synthetic fixtures must not call AWS')), \
             patch('billing.collector.Session', side_effect=AssertionError('Use the fake Cost Explorer')):
            command.generate(options)
            sources = list(BillingSource.objects.all())
            self.assertTrue(any(source.shared for source in sources))
            self.assertTrue(any(source.kind == BillingSource.STANDALONE for source in sources))
            for source in sources:
                self.assertTrue(approval_ready(source), source.account_id)
                self.assertEqual(scheduler.collection_readiness(source), '', source.account_id)
                self.assertEqual(source.trust_checks, {
                    'correct_external_id': 'passed', 'missing_external_id': 'denied', 'wrong_external_id': 'denied',
                    'account_identity': 'passed', 'exact_collector_principal': 'passed',
                    'connection_version': source.connection_version,
                })
                approval = CustomerApproval.objects.get(customer=source.customer)
                self.assertTrue(set(source.accounts.values_list('account_id', flat=True)).issubset(approval.expected_accounts))
                self.assertIn('Synthetic fixture only', approval.evidence)
            result = command.simulate_collection(options)
            self.assertEqual(result['sources'], len(sources))
            self.assertGreater(result['rows_published'], 0)
            fairness = command.measure_fairness(options)
            self.assertGreater(fairness['jobs_scheduled'], 0)
            self.assertEqual(fairness['collect_jobs'], len(sources))
            self.assertEqual(fairness['distinct_sources_leased'], len(sources))
            command.cleanup()
        self.assertFalse(Customer.objects.exists())
        self.assertFalse(CustomerApproval.objects.exists())
        self.assertFalse(RoleApproval.objects.exists())

    def test_fixture_trust_checks_must_match_the_current_connection(self):
        command = SyntheticLoad()
        options = {'customers': 1, 'accounts': 1, 'months': 1, 'services': 1}
        with patch('boto3.client', side_effect=AssertionError('Synthetic fixtures must not call AWS')):
            command.generate(options)
            source = BillingSource.objects.get()
            self.assertEqual(scheduler.collection_readiness(source), '')
            valid_checks = source.trust_checks.copy()
            for checks in ({}, {**valid_checks, 'connection_version': source.connection_version - 1}):
                with self.subTest(checks=checks):
                    source.trust_checks = checks
                    self.assertEqual(scheduler.collection_readiness(source),
                                     'Verify the current connection settings before pulling data.')

    def test_approval_fixture_rejects_non_synthetic_customer(self):
        _, source = make_customer('Real customer', '123456789012')
        original_checks = source.trust_checks.copy()
        with self.assertRaises(CommandError):
            SyntheticLoad().approve_fixture_source(source, [source.account_id])
        source.refresh_from_db()
        self.assertEqual(source.trust_checks, original_checks)
        self.assertFalse(CustomerApproval.objects.exists())
        self.assertFalse(RoleApproval.objects.exists())
