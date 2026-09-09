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
    current=BillingSource.objects.only('ownership_version').get(pk=source.pk)
    return digest([source.role_arn,str(source.external_id),source.connection_version,current.ownership_version])


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
    parameters = scoped_parameters(parameters, account_filter)
    from django.conf import settings
    from .access import current_access, scope_fingerprint
    access = current_access.get()
    if settings.ENFORCE_CUSTOMER_AUTHORIZATION and access and not access.portfolio:
        if customer is None or customer.pk not in access.customers or account_filter is None:
            raise ValueError('An explicit authorized customer and account scope is required.')
        from .models import AccountAssignment
        owned=set(AccountAssignment.objects.filter(customer=customer,account__source=source).values_list('account__account_id',flat=True))
        if not set(account_filter).issubset(owned):
            raise ValueError('The requested accounts are outside the authorized assignment scope.')
    optional = {'get_tags':'tags','get_cost_categories':'cost_categories','get_cost_forecast':'forecasts','get_cost_and_usage_with_resources':'resources'}
    capability = optional.get(operation)
    if operation=='get_dimension_values' and parameters.get('Dimension')=='RESOURCE_ID':capability='resources'
    if settings.REQUIRE_CONNECTION_APPROVAL and capability and not source.capabilities.get(capability):
        raise ValueError(f'{capability}: this optional capability is not approved and available. Core daily/monthly billing is available.')
    if settings.REQUIRE_CONNECTION_APPROVAL:
        from .models import CustomerApproval
        needed=set()
        def inspect(value):
            if isinstance(value,dict):
                for key,item in value.items():
                    if key in ('Tags','CostCategories') and isinstance(item,dict):
                        needed.add(('tags' if key=='Tags' else 'cost_categories',item.get('Key','')))
                    inspect(item)
                if value.get('Type') in ('TAG','COST_CATEGORY'):
                    needed.add(('tags' if value['Type']=='TAG' else 'cost_categories',value.get('Key','')))
            elif isinstance(value,list):
                for item in value:inspect(item)
        inspect(parameters)
        if operation=='get_tags' and parameters.get('TagKey'):needed.add(('tags',parameters['TagKey']))
        if operation=='get_cost_categories' and parameters.get('CostCategoryName'):needed.add(('cost_categories',parameters['CostCategoryName']))
        if needed:
            approval=CustomerApproval.objects.filter(customer=customer or source.customer,status='approved').first()
            for cap,key in needed:
                approved_key=('tag:' if cap=='tags' else 'category:')+key
                if not source.capabilities.get(cap) or not approval or approved_key not in approval.metadata:
                    raise ValueError('The selected metadata key requires customer approval and a verified optional capability.')
    identity = connection_key(source)
    acl = scope_fingerprint(access) if access and settings.ENFORCE_CUSTOMER_AUTHORIZATION else ''
    fingerprint = digest([identity, operation, parameters, str(customer.pk) if customer else '', acl])
    query, _ = ExplorerQuery.objects.get_or_create(source=source, fingerprint=fingerprint,
        defaults={'operation': operation, 'parameters': parameters, 'connection_fingerprint': identity, 'scope_fingerprint':acl, 'customer': customer, 'requested_by_id':access.user_id if access and settings.ENFORCE_CUSTOMER_AUTHORIZATION else None})
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
            return f'{code}: Review the selected report parameters and capability prerequisites.'
        if code in ('AccessDenied', 'AccessDeniedException') and 'opt-in' in message.lower():
            return 'AWS granular data is not enabled. Enable the requested hourly/resource data in the payer account Cost Explorer settings to use this report.'
        if code in ('AccessDenied', 'AccessDeniedException'):
            return 'Billing permission missing. Review the missing capability with the customer administrator using the manual IAM guide.'
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


def request_authorized(query):
    from django.conf import settings
    from .models import CustomerApproval
    if settings.REQUIRE_CONNECTION_APPROVAL and query.customer_id and not CustomerApproval.objects.filter(customer_id=query.customer_id,status='approved').exists():
        return False
    if query.requested_by_id:
        from .access import for_user, scope_fingerprint
        from django.contrib.auth.models import User
        user=User.objects.only('id','username','is_active','is_superuser','is_staff').filter(pk=query.requested_by_id).first()
        if not user or not user.is_active:
            return False
        access=for_user(user)
        return query.scope_fingerprint == scope_fingerprint(access) and (access.portfolio or query.customer_id in access.customers)
    return query.customer_id is None or query.customer.active


def run_query(query, client=None, meter=None):
    source = query.source
    if not request_authorized(query):
        ExplorerQuery.objects.filter(pk=query.pk).update(requested=False,data={},error='Requesting user access changed or was revoked.')
        return
    if source is None or not source.enabled or not source.role_arn or connection_key(source) != query.connection_fingerprint:
        ExplorerQuery.objects.filter(pk=query.pk).update(requested=False, error='Connection changed or collection is paused.')
        return
    started = timezone.now()
    ExplorerQuery.objects.filter(pk=query.pk).update(last_attempt=started)
    try:
        data = fetch_pages(client or Session(source).client('ce'), query.operation, query.parameters, meter)
        if query.operation in ('get_tags','get_cost_categories'):
            from .models import CustomerApproval
            from django.conf import settings
            if settings.REQUIRE_CONNECTION_APPROVAL:
                approval=CustomerApproval.objects.filter(customer=query.customer or source.customer,status='approved').first()
                allowed=approval.metadata if approval else []
                if query.operation=='get_tags' and not query.parameters.get('TagKey'):
                    data['Tags']=[k for k in data.get('Tags',[]) if 'tag:'+k in allowed]
                if query.operation=='get_cost_categories' and not query.parameters.get('CostCategoryName'):
                    data['CostCategoryNames']=[k for k in data.get('CostCategoryNames',[]) if 'category:'+k in allowed]
        with transaction.atomic():
            current = BillingSource.objects.select_for_update().get(pk=source.pk)
            if not current.enabled or connection_key(current) != query.connection_fingerprint or not request_authorized(query):
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
            if not request_authorized(query):
                ExplorerQuery.objects.filter(pk=query.pk).update(requested=False,data={},error='Requesting user access changed or was revoked.')
                continue
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
