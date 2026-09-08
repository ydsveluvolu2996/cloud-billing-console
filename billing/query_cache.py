"""Durable request queue serviced by the existing six-hour/minute cron worker."""
import hashlib
import json
import logging
from datetime import timedelta
from django.db import transaction
from django.utils import timezone
from botocore.exceptions import ClientError
from .collector import cost_client, safe_error
from .models import Customer, ExplorerQuery

logger=logging.getLogger(__name__)
ALLOWED = {'get_cost_and_usage','get_cost_and_usage_with_resources','get_cost_forecast','get_dimension_values','get_tags','get_cost_categories'}


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def connection_key(customer):
    return digest([customer.role_arn,str(customer.external_id)])


def get_query(customer, operation, parameters):
    if operation not in ALLOWED: raise ValueError('Unsupported billing operation.')
    identity=connection_key(customer)
    fingerprint=digest([identity,operation,parameters])
    query,created=ExplorerQuery.objects.get_or_create(customer=customer,fingerprint=fingerprint,
        defaults={'operation':operation,'parameters':parameters,'connection_fingerprint':identity})
    now=timezone.now()
    # New requests queue once. Failed requests have a cooldown to avoid costly reload loops.
    due=not query.last_attempt or query.last_attempt<now-timedelta(hours=6)
    ExplorerQuery.objects.filter(pk=query.pk).update(last_used=now,requested=query.requested or due)
    query.requested=query.requested or due
    return query


def request_error(exc):
    if isinstance(exc,ClientError):
        code=exc.response.get('Error',{}).get('Code','AWS error')
        if code in ('ValidationException','ValidationError'):
            # AWS validation describes unsupported combinations and opt-in prerequisites;
            # never contains credentials and is escaped by Django/JSON textContent.
            return f'{code}: '+exc.response['Error'].get('Message','Review the selected report parameters.')[:440]
        if code in ('AccessDenied','AccessDeniedException') and 'opt-in' in exc.response['Error'].get('Message','').lower():
            return 'AWS granular data is not enabled. Enable the requested hourly/resource data in the payer account Cost Explorer settings to use this report.'
        if code in ('AccessDenied','AccessDeniedException'):
            return 'Billing permission missing. Update the customer CloudFormation role using the current dashboard template.'
        if code=='DataUnavailableException':
            return 'AWS has no data for this request yet. Forecasts need sufficient history; resource/hourly data needs AWS Cost Explorer opt-in.'
    return safe_error(exc)


def fetch_pages(client,operation,parameters):
    if operation not in ALLOWED:raise ValueError('Unsupported billing operation.')
    request=dict(parameters); result={}; seen=set()
    for _ in range(1000):
        page=getattr(client,operation)(**request)
        for key,value in page.items():
            if key in ('NextPageToken','ResponseMetadata'):continue
            if key in ('ResultsByTime','DimensionValues','Tags','CostCategoryNames','CostCategoryValues'):
                result.setdefault(key,[]).extend(value)
            else:result[key]=value
        token=page.get('NextPageToken')
        if not token:return result
        if token in seen:raise ValueError('Repeated AWS page token.')
        seen.add(token);request['NextPageToken']=token
    raise ValueError('Report exceeds the page limit; narrow its date range or filters.')


def run_query(query, client=None):
    customer=query.customer
    if not customer.enabled or not customer.role_arn or connection_key(customer)!=query.connection_fingerprint:
        ExplorerQuery.objects.filter(pk=query.pk).update(requested=False,error='Customer connection changed or collection is paused.')
        return
    started=timezone.now()
    ExplorerQuery.objects.filter(pk=query.pk).update(last_attempt=started)
    try:
        data=fetch_pages(client or cost_client(customer),query.operation,query.parameters)
        with transaction.atomic():
            current=Customer.objects.select_for_update().get(pk=customer.pk)
            if not current.enabled or connection_key(current)!=query.connection_fingerprint:raise ValueError('Connection changed during collection.')
            ExplorerQuery.objects.filter(pk=query.pk).update(data=data,last_success=timezone.now(),error='',requested=False)
    except Exception as exc:
        ExplorerQuery.objects.filter(pk=query.pk).update(error=request_error(exc),requested=False)
        logger.warning('Explorer request failed query=%s type=%s',query.pk,type(exc).__name__)


def refresh_queries(customer_id=None, scheduled=False):
    now=timezone.now()
    active=ExplorerQuery.objects.filter(customer__enabled=True,last_used__gte=now-timedelta(days=7)).exclude(customer__role_arn='')
    if customer_id:active=active.filter(customer_id=customer_id)
    if scheduled:
        active.filter(last_attempt__lt=now-timedelta(hours=5,minutes=55)).update(requested=True)
    # Bound a batch so one customer cannot starve normal imports. Remaining work
    # stays queued for the next minute. No AWS calls occur in web request workers.
    clients={}
    for query in active.filter(requested=True).select_related('customer').order_by('last_attempt','pk')[:100]:
        try:
            client=clients.get(query.customer_id)
            if client is None:
                client=cost_client(query.customer);clients[query.customer_id]=client
            run_query(query,client)
        except Exception as exc:
            ExplorerQuery.objects.filter(pk=query.pk).update(requested=False,last_attempt=now,error=request_error(exc))
    # Expired unused cache is disposable; only query definitions in saved reports persist.
    ExplorerQuery.objects.filter(last_used__lt=now-timedelta(days=30)).delete()
