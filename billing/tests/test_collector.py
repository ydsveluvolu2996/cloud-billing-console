"""Collection, discovery, pagination, transfers, shared payers and AWS budget import."""
from datetime import date, datetime, timedelta, timezone as dt_tz
from decimal import Decimal
from unittest.mock import Mock
from botocore.exceptions import ClientError
from django.test import TestCase
from django.utils import timezone
from billing.aws import Meter, RequestBudgetExceeded, paginate
from billing.collector import collect_source, discover_accounts, ensure_assignment, import_budgets, months_back
from billing.models import AccountAssignment, AwsAccount, CollectionPeriod, Cost, ImportedBudget
from billing.reporting import report
from .helpers import FakeSession, assign, ce_client, ce_page, cost, make_customer


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

    def test_approved_new_account_in_billing_data_assigned_for_non_shared_payer(self):
        from billing.models import CustomerApproval
        CustomerApproval.objects.create(customer=self.customer,status='approved',expected_accounts=['444444444444'])
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
