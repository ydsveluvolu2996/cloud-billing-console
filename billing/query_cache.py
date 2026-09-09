"""Durable per-connection AWS request cache serviced by the worker.

Every cached query belongs to one billing source and, optionally, one customer scope. Customer
scoped queries under shared payers always carry a LINKED_ACCOUNT filter derived from the
account assignments, so a manipulated customer ID cannot widen the AWS request.
"""
import hashlib
import json
import logging
from datetime import timedelta
from botocore.exceptions import ClientError
from django.db import transaction
from django.utils import timezone
from .aws import Meter, Session
from .collector import safe_error
from .models import BillingSource, ExplorerQuery

logger = logging.getLogger(__name__)
ALLOWED = {'get_cost_and_usage', 'get_cost_and_usage_with_resources', 'get_cost_forecast', 'get_dimension_values', 'get_tags', 'get_cost_categories'}
LIST_KEYS = ('ResultsByTime', 'DimensionValues', 'Tags', 'CostCategoryNames', 'CostCategoryValues', 'ForecastResultsByTime')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()


def connection_key(source):
    return digest([source.role_arn, str(source.external_id), source.connection_version])


def scoped_parameters(parameters, account_filter):
    """Add a LINKED_ACCOUNT restriction to a Cost Explorer request when the scope needs one."""
    if account_filter is None:
        return parameters
    params = json.loads(json.dumps(parameters, default=str))
    restriction = {'Dimensions': {'Key': 'LINKED_ACCOUNT', 'Values': sorted(account_filter) or ['000000000000']}}
    if 'Filter' in params:
        params['Filter'] = {'And': [params['Filter'], restriction]}
    else:
        params['Filter'] = restriction
    return params


def get_query(source, operation, parameters, customer=None, account_filter=None):
    if operation not in ALLOWED:
        raise ValueError('Unsupported billing operation.')
    if operation in ('get_cost_and_usage', 'get_cost_and_usage_with_resources', 'get_cost_forecast'):
        parameters = scoped_parameters(parameters, account_filter)
    identity = connection_key(source)
    fingerprint = digest([identity, operation, parameters, str(customer.pk) if customer else ''])
    query, _ = ExplorerQuery.objects.get_or_create(source=source, fingerprint=fingerprint,
        defaults={'operation': operation, 'parameters': parameters, 'connection_fingerprint': identity, 'customer': customer})
    now = timezone.now()
    # New requests queue once. Failed requests have a cooldown to avoid costly reload loops.
    due = not query.last_attempt or query.last_attempt < now - timedelta(hours=6)
    ExplorerQuery.objects.filter(pk=query.pk).update(last_used=now, requested=query.requested or due)
    query.requested = query.requested or due
    if query.requested:
        from .jobs import enqueue
        enqueue('explorer_refresh', key=f'explorer_refresh:{source.pk}:ondemand', source=source, priority=4)
    return query


def request_error(exc):
    if isinstance(exc, ClientError):
        code = exc.response.get('Error', {}).get('Code', 'AWS error')
        message = exc.response['Error'].get('Message', '')
        if code in ('ValidationException', 'ValidationError'):
            # AWS validation describes unsupported combinations and opt-in prerequisites;
            # never contains credentials and is escaped by Django/JSON textContent.
            return f'{code}: ' + (message or 'Review the selected report parameters.')[:440]
        if code in ('AccessDenied', 'AccessDeniedException') and 'opt-in' in message.lower():
            return 'AWS granular data is not enabled. Enable the requested hourly/resource data in the payer account Cost Explorer settings to use this report.'
        if code in ('AccessDenied', 'AccessDeniedException'):
            return 'Billing permission missing. Update the customer CloudFormation role using the current dashboard template.'
        if code == 'DataUnavailableException':
            return 'AWS has no data for this request yet. Forecasts need sufficient history; resource/hourly data needs AWS Cost Explorer opt-in.'
    return safe_error(exc)


def fetch_pages(client, operation, parameters, meter=None):
    if operation not in ALLOWED:
        raise ValueError('Unsupported billing operation.')
    meter = meter or Meter(limit=0)
    request = dict(parameters)
    result = {}
    seen = set()
    for _ in range(1000):
        page = meter.call(client, operation, **request)
        for key, value in page.items():
            if key in ('NextPageToken', 'ResponseMetadata'):
                continue
            if key in LIST_KEYS:
                result.setdefault(key, []).extend(value)
            else:
                result[key] = value
        token = page.get('NextPageToken')
        if not token:
            return result
        if token in seen:
            raise ValueError('Repeated AWS page token.')
        seen.add(token)
        request['NextPageToken'] = token
    raise ValueError('Report exceeds the page limit; narrow its date range or filters.')


def run_query(query, client=None, meter=None):
    source = query.source
    if source is None or not source.enabled or not source.role_arn or connection_key(source) != query.connection_fingerprint:
        ExplorerQuery.objects.filter(pk=query.pk).update(requested=False, error='Connection changed or collection is paused.')
        return
    started = timezone.now()
    ExplorerQuery.objects.filter(pk=query.pk).update(last_attempt=started)
    try:
        data = fetch_pages(client or Session(source).client('ce'), query.operation, query.parameters, meter)
        with transaction.atomic():
            current = BillingSource.objects.select_for_update().get(pk=source.pk)
            if not current.enabled or connection_key(current) != query.connection_fingerprint:
                raise ValueError('Connection changed during collection.')
            ExplorerQuery.objects.filter(pk=query.pk).update(data=data, last_success=timezone.now(), error='', requested=False)
    except Exception as exc:
        ExplorerQuery.objects.filter(pk=query.pk).update(error=request_error(exc), requested=False)
        logger.warning('Explorer request failed query=%s type=%s', query.pk, type(exc).__name__)


def refresh_queries(source=None, scheduled=False, client=None, limit=100):
    """Run requested queries for one source (worker job) or all sources (compatibility path)."""
    now = timezone.now()
    active = ExplorerQuery.objects.filter(source__enabled=True, last_used__gte=now - timedelta(days=7)).exclude(source__role_arn='')
    if source is not None:
        active = active.filter(source=source)
    if scheduled:
        active.filter(last_attempt__lt=now - timedelta(hours=5, minutes=55)).update(requested=True)
    processed = 0
    clients = {}
    meters = {}
    # Bound a batch so one connection cannot starve others. Remaining work stays queued.
    for query in active.filter(requested=True).select_related('source').order_by('last_attempt', 'pk')[:limit]:
        try:
            ce = client or clients.get(query.source_id)
            if ce is None:
                ce = Session(query.source).client('ce')
                clients[query.source_id] = ce
            run_query(query, ce, meters.setdefault(query.source_id, Meter()))
            processed += 1
        except Exception as exc:
            ExplorerQuery.objects.filter(pk=query.pk).update(requested=False, last_attempt=now, error=request_error(exc))
    # Expired unused cache is disposable; only query definitions in saved reports persist.
    ExplorerQuery.objects.filter(last_used__lt=now - timedelta(days=30)).delete()
    return processed
