"""Atomic, repeatable Cost Explorer collection. No AWS credentials are persisted."""
import logging
from datetime import date, timedelta
from decimal import Decimal
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from .models import Cost, Customer, SyncRun

logger = logging.getLogger(__name__)
AWS_CONFIG = Config(retries={'mode': 'standard', 'max_attempts': 5}, connect_timeout=10, read_timeout=60)


class OverlappingBillingScope(Exception):
    pass


def cost_client(customer):
    customer.full_clean()
    sts = boto3.client('sts', region_name=settings.AWS_REGION, config=AWS_CONFIG)
    response = sts.assume_role(RoleArn=customer.role_arn, RoleSessionName=f'billing-{customer.id.hex[:16]}',
                               ExternalId=str(customer.external_id), DurationSeconds=3600)
    cred = response['Credentials']
    return boto3.client('ce', region_name='us-east-1', config=AWS_CONFIG,
        aws_access_key_id=cred['AccessKeyId'], aws_secret_access_key=cred['SecretAccessKey'], aws_session_token=cred['SessionToken'])


def safe_error(exc):
    if isinstance(exc, OverlappingBillingScope):
        return 'An imported account is already assigned to another customer for this period. Resolve the overlapping payer/account connection before retrying.'
    if isinstance(exc, ClientError):
        code = exc.response.get('Error', {}).get('Code', 'AWS error')
        explanations = {
            'AccessDenied': 'Check the role trust policy, external ID, and read-only cost permission.',
            'AccessDeniedException': 'Check the role trust policy and read-only cost permission.',
            'DataUnavailableException': 'AWS is preparing billing data. Enable Cost Explorer and try again later.',
            'OptInRequired': 'Enable Cost Explorer in the customer’s AWS console.',
            'LimitExceededException': 'AWS request limit reached; collection will retry on the next run.',
            'ExpiredToken': 'Temporary AWS credentials expired; retry the connection.',
        }
        return f'{code}: {explanations.get(code, "AWS could not complete the request. Check account access and retry.")}'
    return 'Collection failed. Previous data has been retained; check application logs.'


def verify(customer):
    try:
        ce = cost_client(customer)
        today = timezone.now().date()
        ce.get_cost_and_usage(TimePeriod={'Start': (today - timedelta(days=2)).isoformat(), 'End': today.isoformat()},
                             Granularity='DAILY', Metrics=['UnblendedCost'])
        customer.verified_at = timezone.now()
        customer.last_error = ''
        customer.save(update_fields=['verified_at', 'last_error'])
        return True
    except Exception as exc:
        customer.last_error = safe_error(exc)
        customer.save(update_fields=['last_error'])
        logger.warning('Verification failed customer=%s type=%s', customer.id, type(exc).__name__)
        return False


def month_windows(start, end):
    while start < end:
        stop = min(start.replace(day=1) + relativedelta(months=1), end)
        yield start, stop
        start = stop


def sync_customer(customer, client=None, today=None, full=False):
    if not customer.enabled or not customer.role_arn:
        return None
    today = today or timezone.now().date()
    start = today.replace(day=1) - relativedelta(months=settings.HISTORY_MONTHS if full or not customer.last_success else 1)
    end = today + timedelta(days=1)
    run = SyncRun.objects.create(customer=customer)
    Customer.objects.filter(pk=customer.pk).update(last_attempt=run.started_at)
    records = []
    try:
        ce = client or cost_client(customer)
        for first, last in month_windows(start, end):
            params = dict(TimePeriod={'Start': first.isoformat(), 'End': last.isoformat()}, Granularity='DAILY',
                          Metrics=['UnblendedCost', 'AmortizedCost'],
                          GroupBy=[{'Type': 'DIMENSION', 'Key': 'LINKED_ACCOUNT'}, {'Type': 'DIMENSION', 'Key': 'SERVICE'}])
            seen_tokens = set()
            while True:
                result = ce.get_cost_and_usage(**params)
                for period in result.get('ResultsByTime', []):
                    day = date.fromisoformat(period['TimePeriod']['Start'])
                    if not first <= day < last:
                        raise ValueError('AWS returned a date outside the requested interval')
                    for group in period.get('Groups', []):
                        unblended = group['Metrics']['UnblendedCost']
                        amortized = group['Metrics']['AmortizedCost']
                        if unblended['Unit'] != amortized['Unit']:
                            raise ValueError('Cost metric currencies do not match')
                        records.append(Cost(customer=customer, day=day, account_id=group['Keys'][0],
                            service=group['Keys'][1], currency=unblended['Unit'], unblended=Decimal(unblended['Amount']),
                            amortized=Decimal(amortized['Amount']), estimated=period.get('Estimated', True)))
                token = result.get('NextPageToken')
                if not token:
                    break
                if token in seen_tokens:
                    raise ValueError('Repeated AWS pagination token')
                seen_tokens.add(token)
                params['NextPageToken'] = token
        with transaction.atomic():
            locked = Customer.objects.select_for_update().get(pk=customer.pk)
            if not locked.enabled or locked.role_arn != customer.role_arn:
                raise ValueError('Customer connection changed during collection')
            accounts = {record.account_id for record in records}
            if Cost.objects.exclude(customer=customer).filter(account_id__in=accounts, day__gte=start, day__lt=end).exists():
                raise OverlappingBillingScope()
            Cost.objects.filter(customer=customer, day__gte=start, day__lt=end).delete()
            Cost.objects.bulk_create(records, batch_size=1000)
            Customer.objects.filter(pk=customer.pk).update(last_success=timezone.now(), last_error='', verified_at=timezone.now())
            run.status = 'success'
            run.rows = len(records)
            run.finished_at = timezone.now()
            run.save()
        return run
    except Exception as exc:
        run.status = 'failed'
        run.error = safe_error(exc)
        run.finished_at = timezone.now()
        run.save()
        Customer.objects.filter(pk=customer.pk).update(last_error=run.error)
        logger.warning('Sync failed customer=%s type=%s', customer.id, type(exc).__name__)
        return run
