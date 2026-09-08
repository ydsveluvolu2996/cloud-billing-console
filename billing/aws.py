"""Temporary-credential AWS access with request metering and throttling.

Credentials come from STS AssumeRole with the connection's unique external ID and are held
only in memory for the duration of a job. Nothing here persists access keys or tokens.
"""
import logging
import random
import time
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from django.conf import settings

logger = logging.getLogger(__name__)
AWS_CONFIG = Config(retries={'mode': 'standard', 'max_attempts': 5}, connect_timeout=10, read_timeout=60)
THROTTLE_CODES = {'LimitExceededException', 'Throttling', 'ThrottlingException', 'TooManyRequestsException', 'RequestLimitExceeded'}
# Published Cost Explorer API pricing used for estimates only (USD per paginated request).
CE_REQUEST_PRICE_USD = 0.01


class RequestBudgetExceeded(Exception):
    user_message = 'The AWS request limit for this job was reached. Remaining work continues on the next run.'


class Meter:
    """Counts requests/pages per job, enforces a per-job request cap and paces calls."""

    def __init__(self, limit=None, interval=None):
        self.limit = limit if limit is not None else getattr(settings, 'MAX_REQUESTS_PER_JOB', 400)
        self.interval = interval if interval is not None else getattr(settings, 'AWS_REQUEST_INTERVAL_SECONDS', 0.0)
        self.requests = 0
        self.pages = 0
        self.throttled = 0

    def call(self, client, operation, **kwargs):
        if self.limit and self.requests >= self.limit:
            raise RequestBudgetExceeded()
        attempts = 0
        while True:
            self.requests += 1
            try:
                if self.interval:
                    time.sleep(self.interval)
                result = getattr(client, operation)(**kwargs)
                self.pages += 1
                return result
            except ClientError as exc:
                code = exc.response.get('Error', {}).get('Code', '')
                attempts += 1
                if code in THROTTLE_CODES and attempts <= getattr(settings, 'AWS_THROTTLE_RETRIES', 3):
                    self.throttled += 1
                    time.sleep(min(2 ** attempts, 30) * random.uniform(0.5, 1.5) * getattr(settings, 'AWS_THROTTLE_SLEEP_FACTOR', 1.0))
                    continue
                raise

    @property
    def estimated_cost_usd(self):
        return self.requests * CE_REQUEST_PRICE_USD


class Session:
    """Assumed-role session for one billing source. Credentials never leave memory."""

    def __init__(self, source, credentials=None):
        source.full_clean(exclude=['customer'])
        self.source = source
        if credentials is None:
            sts = boto3.client('sts', region_name=settings.AWS_REGION, config=AWS_CONFIG)
            response = sts.assume_role(RoleArn=source.role_arn, RoleSessionName=f'billing-{source.pk.hex[:16]}',
                                       ExternalId=str(source.external_id), DurationSeconds=3600)
            credentials = response['Credentials']
            self.assumed_account = response.get('AssumedRoleUser', {}).get('Arn', '').split(':')[4] if response.get('AssumedRoleUser') else ''
        else:
            self.assumed_account = credentials.get('Account', '')
        self._credentials = credentials

    def client(self, service, region=None):
        region = region or ('us-east-1' if service in ('ce', 'organizations', 'budgets') else settings.AWS_REGION)
        return boto3.client(service, region_name=region, config=AWS_CONFIG,
                            aws_access_key_id=self._credentials['AccessKeyId'], aws_secret_access_key=self._credentials['SecretAccessKey'],
                            aws_session_token=self._credentials['SessionToken'])


def paginate(meter, client, operation, result_key, token_in='NextToken', token_out='NextToken', **kwargs):
    """Follow pagination tokens, including tokens returned with an empty page."""
    request = dict(kwargs)
    seen = set()
    items = []
    for _ in range(getattr(settings, 'MAX_PAGES_PER_REQUEST', 1000)):
        page = meter.call(client, operation, **request)
        items.extend(page.get(result_key) or [])
        token = page.get(token_out)
        if not token:
            return items
        if token in seen:
            raise ValueError('Repeated AWS pagination token')
        seen.add(token)
        request[token_in] = token
    raise ValueError('AWS pagination exceeded the configured page limit')
