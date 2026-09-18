"""Collection, discovery, pagination, transfers, shared payers and AWS budget import."""
from datetime import date, datetime, timedelta, timezone as dt_tz
from decimal import Decimal
from unittest.mock import Mock, patch
from botocore.exceptions import ClientError
from django.test import TestCase, override_settings
from django.utils import timezone
from billing.aws import Meter, RequestBudgetExceeded, paginate
from billing.collector import ConnectionChanged, collect_source, discover_accounts, ensure_assignment, import_budgets, months_back, verify_source
from billing.models import AccountAssignment, AwsAccount, BillingSource, CollectionPeriod, Cost, Customer, CustomerApproval, ImportedBudget, Job, RoleApproval
from billing.reporting import report
from .helpers import FakeSession, assign, ce_client, ce_page, cost, make_customer


@override_settings(REQUIRE_CONNECTION_APPROVAL=False)
class CollectionTests(TestCase):
    def setUp(self):
        self.customer, self.source = make_customer('Payer Co', '111111111111', accounts=('222222222222', '333333333333'))
        self.today = date(2026, 9, 8)

    def test_single_account_cost_scope_is_filtered_on_every_page(self):
        self.source.kind = 'standalone'
        self.source.save(update_fields=['kind'])
        client = ce_client([
            ce_page([('111111111111', 'Amazon EC2', '2')], date(2026, 9, 1), token='next'),
            ce_page([('111111111111', 'Amazon S3', '3')], date(2026, 9, 1)),
        ])
        collect_source(self.source, months=[date(2026, 9, 1)], client=client, today=self.today)
        for call in client.get_cost_and_usage.call_args_list:
            self.assertEqual(call.kwargs['Filter'], {'Dimensions': {'Key': 'LINKED_ACCOUNT', 'Values': ['111111111111']}})
        self.assertEqual(set(Cost.objects.filter(source=self.source).values_list('account_id', flat=True)), {'111111111111'})

    def test_single_account_rejects_broader_aws_result_without_replacing_prior_costs(self):
        self.source.kind = 'standalone'
        self.source.save(update_fields=['kind'])
        old = cost(self.source, date(2026, 9, 1), '8')
        client = ce_client([ce_page([('111111111111', 'Amazon EC2', '2'), ('444444444444', 'Amazon S3', '3')], date(2026, 9, 1))])
        with self.assertRaisesMessage(ValueError, 'outside the approved single-account scope'):
            collect_source(self.source, months=[date(2026, 9, 1)], client=client, today=self.today)
        old.refresh_from_db()
        self.assertEqual(old.unblended, Decimal('8'))
        self.assertFalse(AwsAccount.objects.filter(account_id='444444444444').exists())
        self.assertFalse(Cost.objects.filter(account_id='444444444444').exists())

    def test_payer_with_members_and_zero_cost_account(self):
        pages = [ce_page([('111111111111', 'AWS Support', '10'), ('222222222222', 'Amazon EC2', '100'), ('333333333333', 'Amazon S3', '0')], date(2026, 9, 1))]
        run = collect_source(self.source, months=[date(2026, 9, 1)], client=ce_client(pages), meter=Meter(limit=0), today=self.today)
        self.assertEqual(run.status, 'success')
        self.assertEqual(Cost.objects.filter(customer=self.customer).count(), 3)
        zero = Cost.objects.get(account_id='333333333333')
        self.assertEqual(zero.unblended, Decimal('0'))
        result = report({'start': '2026-09-01', 'end': '2026-09-01'})
        self.assertEqual(result['total'], Decimal('110'))
        # every account in the organization becomes inventory assigned to the customer
        self.assertEqual(AccountAssignment.objects.filter(customer=self.customer, end__isnull=True).count(), 3)

    @override_settings(REQUIRE_CONNECTION_APPROVAL=True)
    def test_approved_new_account_in_billing_data_assigned_for_non_shared_payer(self):
        from django.conf import settings
        CustomerApproval.objects.create(customer=self.customer, status='approved', expected_accounts=[self.source.account_id, '444444444444'],
            contacts=['Finance'], billing_fields=['cost'], storage_region=settings.AWS_REGION, retention_days=365,
            evidence='Approved inventory', approved_by='admin', approved_at=timezone.now())
        RoleApproval.objects.create(source=self.source, role_arn=self.source.role_arn, connection_version=self.source.connection_version,
            status='approved', requested_by='admin', evidence='Approved role', approved_at=timezone.now())
        pages = [ce_page([('444444444444', 'Amazon EC2', '7')], date(2026, 9, 1))]
        collect_source(self.source, months=[date(2026, 9, 1)], client=ce_client(pages), meter=Meter(limit=0), today=self.today)
        self.assertEqual(Cost.objects.get(account_id='444444444444').customer, self.customer)
        self.assertEqual(AwsAccount.objects.get(account_id='444444444444').payer_account_id, '111111111111')

    def test_shared_payer_new_accounts_land_in_review_queue(self):
        shared_customer, shared = make_customer('Shared payer owner', '555555555555', shared=True)
        other, _ = make_customer('Other tenant', '666666666666', connected=False)
        assign('777777777777', other, shared)
        pages = [ce_page([('555555555555', 'AWS Support', '1'), ('777777777777', 'Amazon EC2', '50'), ('888888888888', 'Amazon EC2', '20')], date(2026, 9, 1))]
        collect_source(shared, months=[date(2026, 9, 1)], client=ce_client(pages), meter=Meter(limit=0), today=self.today)
        self.assertEqual(Cost.objects.get(account_id='777777777777').customer, other)
        self.assertIsNone(Cost.objects.get(account_id='888888888888').customer)
        self.assertIsNone(Cost.objects.get(account_id='555555555555').customer)
        from billing.scope import unassigned_accounts
        self.assertEqual(sorted(a.account_id for a in unassigned_accounts()), ['555555555555', '888888888888'])
        # the other tenant sees only its own accounts
        result = report({'start': '2026-09-01', 'end': '2026-09-01', 'customer': str(other.pk)})
        self.assertEqual(result['total'], Decimal('50'))
        portfolio = report({'start': '2026-09-01', 'end': '2026-09-01'})
        self.assertEqual(portfolio['unassigned_total'], Decimal('21'))

    def test_account_transfer_preserves_historical_ownership(self):
        cost(self.source, date(2026, 8, 15), '40', account_id='222222222222')
        new_owner, new_source = make_customer('New owner', '999999999999')
        account = AwsAccount.objects.get(account_id='222222222222')
        ensure_assignment(account, new_owner, start=date(2026, 9, 1))
        pages = [ce_page([('222222222222', 'Amazon EC2', '9')], date(2026, 9, 2))]
        collect_source(new_source, months=[date(2026, 9, 1)], client=ce_client(pages), meter=Meter(limit=0), today=self.today)
        self.assertEqual(Cost.objects.get(day=date(2026, 8, 15)).customer, self.customer)
        self.assertEqual(Cost.objects.get(day=date(2026, 9, 2)).customer, new_owner)
        history = list(account.assignments.order_by('start').values_list('customer_id', 'end'))
        self.assertEqual(history[0], (self.customer.pk, date(2026, 9, 1)))
        self.assertEqual(history[1], (new_owner.pk, None))
        with self.assertRaises(ValueError):
            ensure_assignment(account, self.customer, start=date(2026, 8, 1))

    def test_disappearing_account_keeps_history(self):
        cost(self.source, date(2026, 8, 1), '5', account_id='333333333333')
        orgs = Mock(); orgs.list_accounts.return_value = {'Accounts': [{'Id': '111111111111', 'Name': 'Mgmt', 'State': 'ACTIVE'}, {'Id': '222222222222', 'Name': 'Prod', 'State': 'ACTIVE'}]}
        self.source.capabilities = {'organizations': True}
        discover_accounts(self.source, session=FakeSession(organizations=orgs), meter=Meter(limit=0), today=self.today)
        missing = AwsAccount.objects.get(account_id='333333333333')
        self.assertIsNotNone(missing.missing_since)
        self.assertEqual(Cost.objects.filter(account_id='333333333333').count(), 1)
        self.assertTrue(missing.assignments.filter(end__isnull=True).exists())

    def test_organizations_pagination_follows_empty_page_token_and_uses_state(self):
        orgs = Mock()
        orgs.list_accounts.side_effect = [{'Accounts': [], 'NextToken': 'a'},
                                          {'Accounts': [{'Id': '111111111111', 'Name': 'Mgmt', 'Email': 'm@example.com', 'State': 'ACTIVE', 'Status': 'SUSPENDED', 'JoinedTimestamp': datetime(2020, 1, 1, tzinfo=dt_tz.utc)}], 'NextToken': 'b'},
                                          {'Accounts': [{'Id': '222222222222', 'Name': 'Dev', 'Status': 'SUSPENDED'}]}]
        self.source.capabilities = {'organizations': True}
        found = discover_accounts(self.source, session=FakeSession(organizations=orgs), meter=Meter(limit=0), today=self.today)
        self.assertEqual(orgs.list_accounts.call_count, 3)
        self.assertEqual(orgs.list_accounts.call_args_list[1].kwargs['NextToken'], 'a')
        self.assertEqual(found['111111111111']['state'], 'ACTIVE')  # State wins over retired Status
        self.assertEqual(found['222222222222']['state'], 'SUSPENDED')
        self.source.refresh_from_db()
        self.assertEqual(self.source.discovery_mode, 'organizations')
        self.source.capabilities = {'organizations': True}
        orgs.list_accounts.side_effect = [{'Accounts': [], 'NextToken': 'loop'}, {'Accounts': [], 'NextToken': 'loop'}]
        with self.assertRaises(ValueError):
            discover_accounts(self.source, session=FakeSession(organizations=orgs), meter=Meter(limit=0), today=self.today)

    def test_billing_only_fallback_when_organizations_declined(self):
        ce = Mock()
        ce.get_dimension_values.side_effect = [{'DimensionValues': [{'Value': '222222222222', 'Attributes': {'description': 'Prod'}}], 'NextPageToken': 'p2'},
                                               {'DimensionValues': [{'Value': '333333333333', 'Attributes': {'description': 'Dev'}}]}]
        self.source.capabilities = {'organizations': False, 'organizations_error': 'AccessDeniedException'}
        found = discover_accounts(self.source, session=FakeSession(ce=ce), meter=Meter(limit=0), today=self.today)
        self.assertEqual(set(found), {'111111111111', '222222222222', '333333333333'})
        self.assertEqual(AwsAccount.objects.get(account_id='222222222222').name, 'Prod')
        self.assertEqual(AwsAccount.objects.get(account_id='222222222222').state, 'UNKNOWN')
        self.source.refresh_from_db()
        self.assertEqual(self.source.discovery_mode, 'billing_only')
        self.assertEqual(ce.get_dimension_values.call_args.kwargs['NextPageToken'], 'p2')

    def test_resumable_multi_month_import_and_atomic_period_publication(self):
        old = cost(self.source, date(2026, 8, 3), '99')
        client = ce_client([ce_page([('111111111111', 'Amazon EC2', '1')], date(2026, 7, 1)),
                            ClientError({'Error': {'Code': 'DataUnavailableException'}}, 'GetCostAndUsage')])
        from billing.models import Job
        Job.objects.create(kind='collect', key='resume', source=self.source)
        from billing.jobs import lease
        job = lease('resume-test')
        with self.assertRaises(ClientError):
            collect_source(self.source, months=[date(2026, 7, 1), date(2026, 8, 1), date(2026, 9, 1)], client=client, meter=Meter(limit=0), today=self.today, job=job)
        self.assertEqual(job.progress['completed'], ['2026-07-01'])
        self.assertEqual(Cost.objects.get(pk=old.pk).unblended, 99)  # August kept its previous snapshot
        self.assertEqual(CollectionPeriod.objects.get(month=date(2026, 7, 1)).status, 'complete')
        self.assertEqual(CollectionPeriod.objects.get(month=date(2026, 8, 1)).status, 'failed')
        client = ce_client([ce_page([('111111111111', 'Amazon EC2', '2')], date(2026, 8, 1)), ce_page([('111111111111', 'Amazon EC2', '3')], date(2026, 9, 1))])
        run = collect_source(self.source, months=[date(2026, 7, 1), date(2026, 8, 1), date(2026, 9, 1)], client=client, meter=Meter(limit=0), today=self.today, job=job)
        self.assertEqual(run.status, 'success')
        self.assertEqual(client.get_cost_and_usage.call_count, 2)  # July was not refetched
        self.assertEqual(Cost.objects.filter(day__month=8).get().unblended, Decimal('2'))

    def test_request_budget_limit_and_throttle_retry(self):
        client = Mock()
        client.get_cost_and_usage.side_effect = [ClientError({'Error': {'Code': 'LimitExceededException'}}, 'x'), ce_page([('111111111111', 'Amazon EC2', '1')], date(2026, 9, 1))]
        with self.settings(AWS_THROTTLE_SLEEP_FACTOR=0):
            run = collect_source(self.source, months=[date(2026, 9, 1)], client=client, meter=Meter(limit=0), today=self.today)
        self.assertEqual(run.status, 'success')
        self.assertEqual(run.requests, 2)
        meter = Meter(limit=1)
        client = ce_client([ce_page([('111111111111', 'Amazon EC2', '1')], date(2026, 8, 1)), ce_page([('111111111111', 'Amazon EC2', '1')], date(2026, 9, 1))])
        with self.assertRaises(RequestBudgetExceeded):
            collect_source(self.source, months=[date(2026, 8, 1), date(2026, 9, 1)], client=client, meter=meter, today=self.today)
        self.source.refresh_from_db()
        self.assertIn('request limit', self.source.last_error)

    def test_connection_change_during_collection_discards_results(self):
        from billing.models import BillingSource
        client = Mock()

        def bump(**kwargs):
            BillingSource.objects.filter(pk=self.source.pk).update(connection_version=99)
            return ce_page([('111111111111', 'Amazon EC2', '1')], date(2026, 9, 1))
        client.get_cost_and_usage.side_effect = bump
        with self.assertRaises(Exception):
            collect_source(self.source, months=[date(2026, 9, 1)], client=client, meter=Meter(limit=0), today=self.today)
        self.assertEqual(Cost.objects.count(), 0)

    def test_currency_mismatch_and_out_of_range_dates_rejected(self):
        page = ce_page([('111111111111', 'Amazon EC2', '1')], date(2026, 10, 1))
        with self.assertRaises(ValueError):
            collect_source(self.source, months=[date(2026, 9, 1)], client=ce_client([page]), meter=Meter(limit=0), today=self.today)
        page = ce_page([('111111111111', 'Amazon EC2', '1')], date(2026, 9, 1))
        page['ResultsByTime'][0]['Groups'][0]['Metrics']['AmortizedCost']['Unit'] = 'EUR'
        with self.assertRaises(ValueError):
            collect_source(self.source, months=[date(2026, 9, 1)], client=ce_client([page]), meter=Meter(limit=0), today=self.today)

    def test_months_back_window(self):
        self.assertEqual(months_back(date(2026, 9, 8), 6)[0], date(2026, 3, 1))
        self.assertEqual(len(months_back(date(2026, 9, 8), 6)), 7)

    def test_import_budgets_paginates_and_preserves_filters(self):
        client = Mock()
        client.describe_budgets.side_effect = [
            {'Budgets': [{'BudgetName': 'Monthly', 'BudgetType': 'COST', 'TimeUnit': 'MONTHLY', 'BudgetLimit': {'Amount': '1000', 'Unit': 'USD'},
                          'CostFilters': {'LinkedAccount': ['222222222222']}, 'CalculatedSpend': {'ActualSpend': {'Amount': '412.5', 'Unit': 'USD'}, 'ForecastedSpend': {'Amount': '900', 'Unit': 'USD'}},
                          'TimePeriod': {'Start': datetime(2026, 9, 1, tzinfo=dt_tz.utc), 'End': datetime(2087, 6, 15, tzinfo=dt_tz.utc)}, 'LastUpdatedTime': datetime(2026, 9, 8, tzinfo=dt_tz.utc), 'HealthStatus': {'Status': 'HEALTHY', 'LastUpdatedTime': datetime(2026, 9, 8, tzinfo=dt_tz.utc)}}], 'NextToken': 'n'},
            {'Budgets': [{'BudgetName': 'RI coverage', 'BudgetType': 'RI_COVERAGE', 'TimeUnit': 'MONTHLY', 'BudgetLimit': {'Amount': '80', 'Unit': 'PERCENTAGE'},
                          'CalculatedSpend': {'ActualSpend': {'Amount': '65', 'Unit': 'PERCENTAGE'}}}]}]
        count = import_budgets(self.source, session=FakeSession(budgets=client), meter=Meter(limit=0))
        self.assertEqual(count, 2)
        self.assertEqual(client.describe_budgets.call_args_list[1].kwargs['NextToken'], 'n')
        monthly = ImportedBudget.objects.get(name='Monthly')
        self.assertEqual(monthly.limit_amount, Decimal('1000'))
        self.assertEqual(monthly.filters, {'LinkedAccount': ['222222222222']})
        self.assertEqual(monthly.actual_amount, Decimal('412.5'))
        self.assertEqual(monthly.raw['HealthStatus']['LastUpdatedTime'], '2026-09-08T00:00:00Z')
        self.assertEqual(monthly.owning_account_id, '111111111111')
        coverage = ImportedBudget.objects.get(name='RI coverage')
        self.assertTrue(coverage.is_percentage)
        self.assertFalse(coverage.is_cost_budget)

    def test_paginate_helper_rejects_repeated_tokens(self):
        client = Mock()
        client.list_accounts.side_effect = [{'Accounts': [], 'NextToken': 't'}, {'Accounts': [], 'NextToken': 't'}]
        with self.assertRaises(ValueError):
            paginate(Meter(limit=0), client, 'list_accounts', 'Accounts')


@override_settings(REQUIRE_CONNECTION_APPROVAL=True, AWS_REGION='ap-south-1')
class CollectionAuthorizationTests(TestCase):
    def setUp(self):
        self.customer, self.source = make_customer('Approved account', '123456789012', kind='standalone')
        self.source.approved_capabilities = ['budgets']
        self.source.save(update_fields=['approved_capabilities'])
        self.approval = CustomerApproval.objects.create(customer=self.customer, status='approved',
            expected_accounts=[self.source.account_id], optional_capabilities=['budgets'],
            contacts=['Finance'], billing_fields=['cost'], authorized_users=['admin'],
            storage_region='ap-south-1', retention_days=365, evidence='Approved request',
            approved_by='admin', approved_at=timezone.now())
        self.role = RoleApproval.objects.create(source=self.source, role_arn=self.source.role_arn,
            connection_version=self.source.connection_version, status='approved', requested_by='admin',
            approved_by='admin', approved_at=timezone.now(), evidence='Approved role')
        self.today = date(2026, 9, 8)

    def test_approved_shared_payer_keeps_member_customer_ownership(self):
        self.source.kind, self.source.shared = 'payer', True
        self.source.save(update_fields=['kind', 'shared'])
        member, _ = make_customer('Member customer', '222222222222', connected=False)
        assign('333333333333', member, self.source)
        self.approval.expected_accounts.append('333333333333')
        self.approval.save(update_fields=['expected_accounts'])
        client = ce_client([ce_page([('333333333333', 'Amazon EC2', '12')], date(2026, 9, 1))])
        collect_source(self.source, months=[date(2026, 9, 1)], client=client, today=self.today)
        self.assertEqual(Cost.objects.get(source=self.source).customer, member)

    def test_revoked_or_reduced_consent_during_cost_fetch_keeps_previous_snapshot(self):
        previous = cost(self.source, date(2026, 9, 1), '8')
        changes = (
            lambda: RoleApproval.objects.filter(pk=self.role.pk).update(status='revoked'),
            lambda: CustomerApproval.objects.filter(pk=self.approval.pk).update(status='revoked'),
            lambda: CustomerApproval.objects.filter(pk=self.approval.pk).update(expected_accounts=[]),
            lambda: CustomerApproval.objects.filter(pk=self.approval.pk).update(optional_capabilities=[]),
            lambda: Customer.objects.filter(pk=self.customer.pk).update(active=False),
        )
        for index, change in enumerate(changes):
            with self.subTest(change=index):
                RoleApproval.objects.filter(pk=self.role.pk).update(status='approved')
                CustomerApproval.objects.filter(pk=self.approval.pk).update(status='approved',
                    expected_accounts=[self.source.account_id], optional_capabilities=['budgets'])
                Customer.objects.filter(pk=self.customer.pk).update(active=True)
                def fetch(**kwargs):
                    change()
                    return ce_page([(self.source.account_id, 'Amazon EC2', '99')], date(2026, 9, 1))
                client = Mock()
                client.get_cost_and_usage.side_effect = fetch
                with self.assertRaises(ConnectionChanged):
                    collect_source(self.source, months=[date(2026, 9, 1)], client=client, today=self.today)
                previous.refresh_from_db()
                self.assertEqual(previous.unblended, Decimal('8'))
                self.assertEqual(Cost.objects.filter(source=self.source).count(), 1)

    def test_budget_response_after_revocation_keeps_previous_budgets(self):
        previous = ImportedBudget.objects.create(source=self.source, owning_account_id=self.source.account_id,
            name='Previous budget', budget_type='COST', time_unit='MONTHLY', limit_amount=100, limit_unit='USD')
        def fetch(**kwargs):
            RoleApproval.objects.filter(pk=self.role.pk).update(status='revoked')
            return {'Budgets': [{'BudgetName': 'Replacement', 'BudgetLimit': {'Amount': '200', 'Unit': 'USD'}}]}
        client = Mock()
        client.describe_budgets.side_effect = fetch
        with self.assertRaises(ConnectionChanged):
            import_budgets(self.source, session=FakeSession(budgets=client))
        previous.refresh_from_db()
        self.assertEqual(ImportedBudget.objects.get(source=self.source).pk, previous.pk)
        self.assertEqual(previous.limit_amount, Decimal('100'))

    def test_replaced_budget_worker_cannot_overwrite_new_workers_snapshot(self):
        from billing.jobs import enqueue, lease, recover_expired
        previous = ImportedBudget.objects.create(source=self.source, owning_account_id=self.source.account_id,
            name='Monthly', budget_type='COST', time_unit='MONTHLY', limit_amount=100, limit_unit='USD')
        enqueue('import_budgets', key='lease-fenced-budget', source=self.source)
        old_job = lease('old-budget-worker')
        def fetch(**kwargs):
            Job.objects.filter(pk=old_job.pk).update(lease_expires=timezone.now() - timedelta(seconds=1))
            recover_expired()
            Job.objects.filter(pk=old_job.pk).update(run_after=timezone.now())
            self.assertEqual(lease('replacement-budget-worker').pk, old_job.pk)
            ImportedBudget.objects.filter(pk=previous.pk).update(limit_amount=150)
            return {'Budgets': [{'BudgetName': 'Monthly', 'BudgetLimit': {'Amount': '200', 'Unit': 'USD'}}]}
        client = Mock()
        client.describe_budgets.side_effect = fetch
        with self.assertRaises(ConnectionChanged):
            import_budgets(self.source, session=FakeSession(budgets=client), job=old_job)
        previous.refresh_from_db()
        self.assertEqual(previous.limit_amount, Decimal('150'))
        current_job = Job.objects.get(pk=old_job.pk)
        self.assertEqual(current_job.worker, 'replacement-budget-worker')
        self.assertEqual(current_job.status, Job.LEASED)

    def test_revoked_discovery_cannot_publish_inventory(self):
        self.source.kind = 'payer'
        self.source.save(update_fields=['kind'])
        self.approval.expected_accounts.append('222222222222')
        self.approval.save(update_fields=['expected_accounts'])
        original_discovered_at = self.source.discovered_at
        def fetch(**kwargs):
            CustomerApproval.objects.filter(pk=self.approval.pk).update(status='revoked')
            return {'DimensionValues': [{'Value': '222222222222'}]}
        client = Mock()
        client.get_dimension_values.side_effect = fetch
        with self.assertRaises(ConnectionChanged):
            discover_accounts(self.source, session=FakeSession(ce=client), today=self.today)
        self.source.refresh_from_db()
        self.assertEqual(self.source.discovered_at, original_discovered_at)
        self.assertFalse(AwsAccount.objects.filter(account_id='222222222222').exists())

    def test_stale_verification_cannot_overwrite_current_trust_evidence(self):
        BillingSource.objects.filter(pk=self.source.pk).update(trust_checks={'current': 'preserved'})
        def identity(**kwargs):
            BillingSource.objects.filter(pk=self.source.pk).update(connection_version=2, verified_at=None)
            return {'Account': self.source.account_id}
        sts = Mock()
        sts.get_caller_identity.side_effect = identity
        session = FakeSession(sts=sts, ce=Mock(), budgets=Mock())
        with patch('boto3.client'), \
             patch('billing.iam.verify_trust', return_value={'connection_version': 1}), \
             self.assertRaises(ConnectionChanged):
            verify_source(self.source, session=session)
        self.source.refresh_from_db()
        self.assertEqual(self.source.trust_checks, {'current': 'preserved'})
        self.assertIsNone(self.source.verified_at)

    def test_configuration_change_after_last_month_cannot_mark_new_source_imported(self):
        original_success = self.source.last_success
        job = Mock(pk=None, progress={})
        client = ce_client([ce_page([(self.source.account_id, 'Amazon EC2', '2')], date(2026, 9, 1))])
        def change(*args):
            BillingSource.objects.filter(pk=self.source.pk).update(connection_version=2,
                initial_import_done=False, last_error='Current connection needs verification')
        with patch('billing.jobs.heartbeat', side_effect=change), self.assertRaises(ConnectionChanged):
            collect_source(self.source, months=[date(2026, 9, 1)], client=client, today=self.today, job=job)
        self.source.refresh_from_db()
        self.assertFalse(self.source.initial_import_done)
        self.assertEqual(self.source.last_success, original_success)
        self.assertEqual(self.source.last_error, 'Current connection needs verification')
