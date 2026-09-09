"""Shared fixtures for the billing test suite."""
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import Mock
from django.test import override_settings
from django.utils import timezone
from billing.models import AccountAssignment, AwsAccount, BillingSource, Cost, Customer

ROLE = 'arn:aws:iam::{}:role/BillingConsole/CostReadOnly'
TEST_STORAGES = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
                 'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}
web_settings = override_settings(MFA_REQUIRED=False, ENFORCE_CUSTOMER_AUTHORIZATION=False, REQUIRE_CONNECTION_APPROVAL=False, SECURE_SSL_REDIRECT=False, STORAGES=TEST_STORAGES, ALLOWED_HOSTS=['testserver', 'localhost'])


def make_customer(name, account_id, kind='payer', connected=True, shared=False, currency='USD', accounts=()):
    """Customer with one verified, imported connection and assigned accounts (payer itself + extras)."""
    customer = Customer.objects.create(name=name, currency=currency)
    now = timezone.now()
    source = BillingSource.objects.create(customer=customer, kind=kind, account_id=account_id, shared=shared,
                                          role_arn=ROLE.format(account_id) if connected else '',
                                          verified_at=now if connected else None, discovered_at=now if connected else None,
                                          initial_import_done=connected, last_success=now if connected else None,
                                          onboarding_step=6 if connected else 2)
    if not shared:
        for acct in (account_id, *accounts):
            assign(acct, customer, source)
    return customer, source


def assign(account_id, customer, source=None, start=date(2000, 1, 1), payer=None):
    account, _ = AwsAccount.objects.get_or_create(account_id=account_id, defaults={
        'source': source, 'payer_account_id': payer or (source.account_id if source else ''), 'state': 'ACTIVE'})
    if source and account.source_id is None:
        account.source = source
        account.save()
    open_assignment = account.assignments.filter(end__isnull=True).first()
    if open_assignment and open_assignment.customer_id != customer.pk:
        open_assignment.end = start
        open_assignment.save()
    return AccountAssignment.objects.create(account=account, customer=customer, start=start)


def cost(source, day, amount, account_id=None, service='Amazon EC2', currency='USD', customer='auto', estimated=False):
    if customer == 'auto':
        customer = source.customer
    return Cost.objects.create(source=source, customer=customer, day=day, account_id=account_id or source.account_id, service=service,
                               currency=currency, unblended=Decimal(str(amount)), amortized=Decimal(str(amount)), estimated=estimated)


def ce_page(rows, day, token=None, estimated=True):
    """One GetCostAndUsage page: rows = [(account, service, amount[, unit])]."""
    groups = []
    for row in rows:
        account, service, amount = row[:3]
        unit = row[3] if len(row) > 3 else 'USD'
        groups.append({'Keys': [account, service], 'Metrics': {'UnblendedCost': {'Amount': str(amount), 'Unit': unit},
                                                              'AmortizedCost': {'Amount': str(amount), 'Unit': unit}}})
    page = {'ResultsByTime': [{'TimePeriod': {'Start': str(day), 'End': str(day + timedelta(days=1))}, 'Estimated': estimated, 'Groups': groups}]}
    if token:
        page['NextPageToken'] = token
    return page


def ce_client(pages):
    client = Mock()
    client.get_cost_and_usage.side_effect = list(pages)
    return client


class FakeSession:
    """Stands in for billing.aws.Session; returns preconfigured mock clients per service."""

    def __init__(self, **clients):
        self.clients = clients

    def client(self, service, region=None):
        return self.clients.setdefault(service, Mock())
