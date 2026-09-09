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
from django.db import transaction
from django.utils import timezone
from .models import AuditEvent, AwsAccount, BillingSource, Budget, BudgetAmount, Customer, ExplorerQuery, Job, new_external_id

ACCOUNT_RE = re.compile(r'^\d{12}$')
WIZARD_STEPS = [(1, 'Customer'), (2, 'Connection'), (3, 'Setup link'), (4, 'Verify role'), (5, 'Discover accounts'), (6, 'Assign & import')]


def template_parameters(source):
    return {'CollectorRoleArn': settings.COLLECTOR_ROLE_ARN, 'ExternalId': str(source.external_id), 'ExpectedAccountId': source.account_id,
            'EnableOrganizationsDiscovery': 'true' if source.kind == BillingSource.PAYER else 'false',
            'EnableBudgetImport': 'true' if source.capabilities.get('request_budgets', True) else 'false'}


def source_template(source):
    if not settings.COLLECTOR_ROLE_ARN:
        raise ValueError('The collector IAM role is not configured yet.')
    template = yaml.safe_load((settings.BASE_DIR / 'deploy/customer-role.yaml').read_text())
    for key, value in template_parameters(source).items():
        template['Parameters'][key]['Default'] = value
    return yaml.safe_dump(template, sort_keys=False)


def quick_create_url(source):
    if not settings.ARTIFACT_BUCKET or not settings.COLLECTOR_ROLE_ARN:
        return ''
    # New buckets may redirect the global S3 endpoint. A redirect changes the
    # signed host and invalidates the URL, so always sign the regional endpoint.
    s3 = boto3.client('s3', region_name=settings.AWS_REGION, endpoint_url=f'https://s3.{settings.AWS_REGION}.amazonaws.com',
                      config=Config(signature_version='s3v4', s3={'addressing_style': 'virtual'}))
    # Generic template contains no credentials; presigned URL expires after one hour.
    template_url = s3.generate_presigned_url('get_object', Params={'Bucket': settings.ARTIFACT_BUCKET, 'Key': 'templates/customer-role.yaml'}, ExpiresIn=3600)
    query = {'templateURL': template_url, 'stackName': 'CloudBillingReadOnly'}
    query.update({f'param_{key}': value for key, value in template_parameters(source).items()})
    return f'https://{settings.AWS_REGION}.console.aws.amazon.com/cloudformation/home?region={settings.AWS_REGION}#/stacks/create/review?{urlencode(query)}'


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

CUSTOMER_COLUMNS = ['name', 'reference', 'owner', 'currency', 'account_id', 'kind', 'shared', 'budget']


def parse_customer_csv(text):
    """Validate a customer CSV without writing anything. Returns (rows, errors)."""
    reader = csv.DictReader(io.StringIO(text))
    rows, errors = [], []
    if not reader.fieldnames or 'name' not in reader.fieldnames or 'account_id' not in reader.fieldnames:
        return [], ['The CSV needs at least the columns name and account_id.']
    seen_names, seen_accounts = set(), set()
    for number, raw in enumerate(reader, start=2):
        row = {k: (v or '').strip() for k, v in raw.items() if k}
        problems = []
        name = row.get('name', '')
        if not name:
            problems.append('name is required')
        elif name.lower() in seen_names:
            problems.append('duplicate name in file')
        seen_names.add(name.lower())
        existing = Customer.objects.filter(name__iexact=name).first() if name else None
        account_id = row.get('account_id', '')
        if not ACCOUNT_RE.match(account_id):
            problems.append('account_id must be 12 digits')
        elif account_id in seen_accounts:
            problems.append('duplicate account_id in file')
        else:
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
        rows.append({'line': number, 'name': name, 'reference': row.get('reference', ''), 'owner': row.get('owner', ''), 'currency': currency,
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
            customer, made = Customer.objects.get_or_create(name__iexact=row['name'], defaults={
                'name': row['name'], 'reference': row['reference'], 'owner': row['owner'], 'currency': row['currency']})
            source = BillingSource.objects.create(customer=customer, kind=row['kind'], account_id=row['account_id'], shared=row['shared'], onboarding_step=3)
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
    writer.writerow(['Example Ltd', 'CRM-1001', 'Jane Owner', 'USD', '123456789012', 'payer', 'false', '5000'])
    return out.getvalue()


def budget_csv_template():
    from .budgets import BUDGET_COLUMNS
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(BUDGET_COLUMNS)
    writer.writerow(['Example Ltd', 'customer', 'Example monthly', '5000', 'USD', 'unblended', '', '', '', date.today().replace(day=1).isoformat(), '80', '100'])
    writer.writerow(['Example Ltd', 'account', 'Production account', '3000', 'USD', 'unblended', '', '210987654321', '', '', '80', '100'])
    return out.getvalue()
