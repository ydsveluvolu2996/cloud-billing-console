"""Onboarding: per-connection CloudFormation templates and links, bulk CSV, rotation, offboarding."""
import csv
import io
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlencode
import boto3
import yaml
from botocore.config import Config
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from .models import AuditEvent, AwsAccount, BillingSource, Budget, BudgetAmount, Customer, ExplorerQuery, Job, new_external_id

ACCOUNT_RE = re.compile(r'^\d{12}$')
WIZARD_STEPS = [(1, 'Customer'), (2, 'Connection'), (3, 'Manual IAM role'), (4, 'Verify role'), (5, 'Discover accounts'), (6, 'Assign & import')]


def source_template(source):
    """Download manual IAM JSON; no customer infrastructure is required."""
    import json
    from .iam import policy_bundle
    return json.dumps(policy_bundle(source), indent=2)


def quick_create_url(source):
    # Compatibility for callers: the setup route now renders copyable manual policies.
    from django.urls import reverse
    return reverse('source_setup', args=[source.pk])


def rotate_external_id(source, actor=''):
    """Issue a new external ID and connection version; cached AWS results become stale by design."""
    with transaction.atomic():
        locked = BillingSource.objects.select_for_update().get(pk=source.pk)
        locked.external_id = new_external_id()
        locked.connection_version += 1
        locked.verified_at = None
        locked.last_error = ''
        locked.onboarding_step = 3
        locked.save(update_fields=['external_id', 'connection_version', 'verified_at', 'last_error', 'onboarding_step'])
        ExplorerQuery.objects.filter(source=locked).update(requested=False)
        Job.objects.filter(source=locked, status=Job.QUEUED).delete()
        AuditEvent.objects.create(actor=actor, action='External ID rotated', customer=locked.customer, source=locked)
    return locked


def set_paused(source, paused, actor=''):
    BillingSource.objects.filter(pk=source.pk).update(enabled=not paused, sync_requested=False)
    if paused:
        Job.objects.filter(source=source, status=Job.QUEUED).delete()
    AuditEvent.objects.create(actor=actor, action='Collection paused' if paused else 'Collection resumed', customer=source.customer, source=source)


def offboard_customer(customer, actor=''):
    """Stop collection for every connection but retain all historical records."""
    from .governance import stop_customer
    stop_customer(customer, actor)
    now = timezone.now()
    with transaction.atomic():
        Customer.objects.filter(pk=customer.pk).update(active=False, offboarded_at=now)
        BillingSource.objects.filter(customer=customer).update(enabled=False, sync_requested=False)
        Job.objects.filter(source__customer=customer, status=Job.QUEUED).delete()
        for assignment in customer.assignments.filter(end__isnull=True):
            assignment.end = max(now.date(), assignment.start + timezone.timedelta(days=1))
            assignment.save(update_fields=['end'])
        AuditEvent.objects.create(actor=actor, action='Customer offboarded (history retained)', customer=customer)


def reactivate_customer(customer, actor=''):
    Customer.objects.filter(pk=customer.pk).update(active=True, offboarded_at=None)
    AuditEvent.objects.create(actor=actor, action='Customer reactivated', customer=customer)


def detect_conflicts(account_id, customer=None):
    """Explain why connecting ``account_id`` would duplicate or conflict with existing scope."""
    problems = []
    existing = BillingSource.objects.filter(account_id=account_id, kind__in=[BillingSource.PAYER, BillingSource.STANDALONE]).select_related('customer').first()
    if existing and (customer is None or existing.customer_id != customer.pk):
        problems.append(f'Account {account_id} is already connected for {existing.customer.name}.')
    elif existing:
        problems.append(f'Account {account_id} already has a connection for this customer.')
    member = AwsAccount.objects.filter(account_id=account_id).exclude(payer_account_id='').exclude(payer_account_id=account_id).select_related('source', 'source__customer').first()
    if member and member.source_id:
        problems.append(f'Account {account_id} is a member of payer {member.payer_account_id} ({member.source.customer.name}); a separate connection would collect its costs twice.')
    return problems


# --- bulk CSV --------------------------------------------------------------------------------------

CUSTOMER_COLUMNS = ['customer_id', 'name', 'reference', 'owner', 'currency', 'account_id', 'kind', 'shared', 'budget']


def parse_customer_csv(text):
    """Validate a customer CSV without writing anything. Returns (rows, errors)."""
    reader = csv.DictReader(io.StringIO(text))
    rows, errors = [], []
    if not reader.fieldnames or 'name' not in reader.fieldnames or 'account_id' not in reader.fieldnames:
        return [], ['The CSV needs at least the columns name and account_id.']
    seen_names, seen_accounts = {}, set()
    for number, raw in enumerate(reader, start=2):
        row = {k: (v or '').strip() for k, v in raw.items() if k}
        problems = []
        name = row.get('name', '')
        if not name:
            problems.append('name is required')
        reference = row.get('reference','')
        if name.lower() in seen_names and (not reference or seen_names[name.lower()] != reference):
            problems.append('duplicate name in file without a consistent explicit customer reference')
        seen_names[name.lower()] = reference
        existing = None
        if row.get('customer_id'):
            try:
                existing=Customer.objects.filter(pk=row['customer_id']).first()
            except (ValueError, ValidationError):
                problems.append('customer_id must be a valid customer UUID')
            if existing is None:problems.append('customer_id is not available in your authorized scope')
        elif reference:
            matches=Customer.objects.filter(reference=reference)
            if matches.count()>1:problems.append('reference is ambiguous; specify customer_id')
            else:existing=matches.first()
        named=Customer.objects.filter(name=name).first() if name else None
        if named and existing is None:
            problems.append('existing customer requires its customer_id or unique reference; names do not authorize merging')
        if existing and existing.name!=name:
            problems.append('name differs from the explicitly selected customer')
        account_id = row.get('account_id', '')
        if not ACCOUNT_RE.match(account_id):
            problems.append('account_id must be 12 digits')
        elif account_id in seen_accounts:
            problems.append('duplicate account_id in file')
        else:
            prior=BillingSource.objects.filter(account_id=account_id).first()
            if not (prior and existing and prior.customer_id==existing.pk):
                problems.extend(detect_conflicts(account_id, existing))
        seen_accounts.add(account_id)
        kind = (row.get('kind') or 'payer').lower()
        if kind not in (BillingSource.PAYER, BillingSource.STANDALONE):
            problems.append('kind must be payer or standalone')
        currency = (row.get('currency') or 'USD').upper()
        if len(currency) != 3 or not currency.isalpha():
            problems.append('currency must be a 3-letter code')
        budget = row.get('budget', '')
        if budget:
            try:
                if Decimal(budget) <= 0:
                    problems.append('budget must be positive')
            except InvalidOperation:
                problems.append('budget is not a number')
        rows.append({'line': number, 'customer_id': str(existing.pk) if existing else '', 'name': name, 'reference': row.get('reference', ''), 'owner': row.get('owner', ''), 'currency': currency,
                     'account_id': account_id, 'kind': kind, 'shared': (row.get('shared', '') or 'false').lower() in ('1', 'true', 'yes'),
                     'budget': budget, 'action': 'add connection' if existing else 'create', 'problems': problems})
        errors.extend(f'Line {number}: {p}' for p in problems)
    if not rows:
        errors.append('The file contains no customer rows.')
    return rows, errors


def apply_customer_rows(rows, actor):
    created = []
    with transaction.atomic():
        for row in rows:
            if row['problems']:
                raise ValueError(f'Line {row["line"]} has validation problems.')
            if row.get('customer_id'):
                customer = Customer.objects.select_for_update().get(pk=row['customer_id'])
                made = False
            else:
                customer, made = Customer.objects.get_or_create(name=row['name'], defaults={
                    'reference': row['reference'], 'owner': row['owner'], 'currency': row['currency']})
                if not made and (not row['reference'] or customer.reference != row['reference']):
                    raise ValueError('Customer identity changed after preview. Upload a fresh preview with the explicit customer ID.')
            source, added = BillingSource.objects.get_or_create(account_id=row['account_id'], kind=row['kind'], defaults={
                'customer':customer,'shared':row['shared'],'onboarding_step':3})
            if source.customer_id!=customer.pk or source.shared!=row['shared']:
                raise ValueError('Connection ownership differs from the preview. Resolve the conflict before retrying.')
            if row['budget'] and made:
                budget = Budget.objects.create(customer=customer, scope=Budget.CUSTOMER, name=f'{customer.name} monthly budget', currency=row['currency'], created_by=actor)
                BudgetAmount.objects.create(budget=budget, amount=Decimal(row['budget']), effective_from=date.today().replace(day=1))
            AuditEvent.objects.create(actor=actor, action='Customer bulk onboarded' if made else 'Connection bulk added', customer=customer, source=source)
            created.append(source)
    return created


def customer_csv_template():
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(CUSTOMER_COLUMNS)
    writer.writerow(['', 'Example Ltd', 'CRM-1001', 'Jane Owner', 'USD', '123456789012', 'payer', 'false', '5000'])
    return out.getvalue()


def budget_csv_template():
    from .budgets import BUDGET_COLUMNS
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(BUDGET_COLUMNS)
    writer.writerow(['Example Ltd', 'customer', 'Example monthly', '5000', 'USD', 'unblended', '', '', '', date.today().replace(day=1).isoformat(), '80', '100'])
    writer.writerow(['Example Ltd', 'account', 'Production account', '3000', 'USD', 'unblended', '', '210987654321', '', '', '80', '100'])
    return out.getvalue()
