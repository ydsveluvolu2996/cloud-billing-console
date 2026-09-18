from unittest.mock import patch

from django.core.management.base import CommandError
from django.test import TestCase, override_settings

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
                approval = CustomerApproval.objects.get(customer=source.customer)
                self.assertTrue(set(source.accounts.values_list('account_id', flat=True)).issubset(approval.expected_accounts))
                self.assertIn('Synthetic fixture only', approval.evidence)
            result = command.simulate_collection(options)
            self.assertEqual(result['sources'], len(sources))
            self.assertGreater(result['rows_published'], 0)
            command.cleanup()
        self.assertFalse(Customer.objects.exists())
        self.assertFalse(CustomerApproval.objects.exists())
        self.assertFalse(RoleApproval.objects.exists())

    def test_approval_fixture_rejects_non_synthetic_customer(self):
        _, source = make_customer('Real customer', '123456789012')
        with self.assertRaises(CommandError):
            SyntheticLoad().approve_fixture_source(source, [source.account_id])
        self.assertFalse(CustomerApproval.objects.exists())
        self.assertFalse(RoleApproval.objects.exists())
