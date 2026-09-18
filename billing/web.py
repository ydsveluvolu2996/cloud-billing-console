import csv
from functools import wraps
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import connection
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from django.views.decorators.cache import never_cache
from .models import AuditEvent, BillingSource, Cost, Customer, SyncRun, ExplorerQuery, Job, SavedReport
from .advanced_explorer import build_report
from . import parameters as contract
from . import scheduler
from . import scope as scoping
from .query_cache import get_query
from .reporting import report
from .explorer import defaults as explorer_defaults, explorer_report


def staff_required(view):
    @wraps(view)
    @login_required
    def wrapped(request, *args, **kwargs):
        if not scoping.can_edit(request.user):
            return HttpResponseForbidden('An operator or administrator must perform this action.')
        from .access import editing
        with editing():
            return view(request, *args, **kwargs)
    return wrapped


def paginate(request, items, per_page=25):
    paginator = Paginator(items, per_page)
    try:
        number = max(1, int(request.GET.get('page', 1)))
    except (TypeError, ValueError):
        number = 1
    return paginator.get_page(number)


def audit(request, action, customer=None, source=None, **details):
    from .authentication import security_event
    security_event(request.user.username,action,customer=customer,source=source,**details)


@never_cache
@login_required
def dashboard(request):
    try:
        return render(request, 'billing/explorer.html', build_report(request.GET))
    except ValueError as exc:
        return render(request, 'billing/filter_error.html', {'error': str(exc), 'active_page': 'explorer'}, status=400)


@never_cache
@login_required
def portfolio(request):
    return render_dashboard(request, explorer=False)


def render_dashboard(request, explorer):
    params = explorer_defaults(request.GET) if explorer else request.GET
    try:
        context = report(params)
        if explorer:
            context = explorer_report(context, params)
    except ValueError as exc:
        return render(request, 'billing/filter_error.html', {'error': str(exc), 'active_page': 'portfolio'}, status=400)
    choices = context['scope'].costs(Cost.objects.all())
    context.update({'customers': Customer.objects.filter(active=True),
        'currencies': sorted(set(Cost.objects.values_list('currency', flat=True).distinct()) | {'USD'}),
        'accounts': choices.order_by('account_id').values_list('account_id', flat=True).distinct(),
        'service_options': choices.order_by('service').values_list('service', flat=True).distinct(),
        'active_page': 'explorer' if explorer else 'portfolio'})
    return render(request, 'billing/explorer.html' if explorer else 'billing/dashboard.html', context)


@require_POST
@staff_required
def request_sync(request, pk):
    customer = get_object_or_404(Customer, pk=pk)
    queued = 0
    for source in customer.sources.filter(enabled=True).exclude(role_arn='').exclude(verified_at=None):
        if source.collects_costs:
            scheduler.request_refresh(source, actor=request.user.username)
            queued += 1
    if queued:
        audit(request, 'Sync requested', customer=customer)
        messages.success(request, f'Refresh queued for {queued} connection(s). The worker starts within a minute; large imports may take longer.')
    else:
        messages.error(request, 'Complete onboarding and enable a connection first.')
    return redirect('customer_detail', pk=pk)


@require_POST
@staff_required
def refresh_costs(request):
    # Reuse validated filters; never redirect to an arbitrary submitted URL.
    try:
        data = build_report(request.POST) if request.POST.get('group_by') else report(request.POST)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    sources = scheduler.active_sources()
    if data['filter_customer']:
        units = scoping.report_units(Customer.objects.get(pk=data['filter_customer']))
        sources = sources.filter(pk__in=[u[0].pk for u in units])
    count = 0
    for source in sources:
        scheduler.request_refresh(source, actor=request.user.username)
        count += 1
    ExplorerQuery.objects.filter(source__in=sources, pk__in=data.get('query_ids', [])).update(requested=True)
    if count:
        audit(request, f'Billing refresh requested for {count} connection(s)')
        messages.success(request, f'Refresh queued for {count} connection(s). Collection starts within a minute. Reload the page after the import completes.')
    else:
        messages.error(request, 'Connect an active customer before requesting a refresh.')
    return redirect(('/portfolio/?' if not request.POST.get('group_by') else '/?') + data['export_query'])


def safe_csv(value):
    text = str(value)
    return "'" + text if text.startswith(('=', '+', '-', '@', '\t', '\r')) else text


@never_cache
@login_required
def export_csv(request):
    try:
        data = report(request.GET)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="billing-costs.csv"'
    writer = csv.writer(response)
    writer.writerow(['Date (UTC)', 'Customer', 'Payer connection', 'AWS account', 'Service', 'Currency', 'Unblended cost', 'Amortized cost', 'Estimated'])
    for cost in data['selected'].select_related('customer', 'source').order_by('day', 'customer__name', 'service').iterator():
        writer.writerow([cost.day.isoformat(), safe_csv(cost.customer.name if cost.customer else 'Unassigned'), cost.source.account_id if cost.source else '',
                         cost.account_id, safe_csv(cost.service), cost.currency, str(cost.unblended), str(cost.amortized), cost.estimated])
    writer.writerow([]); writer.writerow(['Scope', safe_csv(data['scope'].label())])
    audit(request, 'Cost report exported')
    return response


@never_cache
@login_required
def export_report(request):
    try:
        data = build_report(request.GET)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    if data.get('report_incomplete'):
        return render(request, 'billing/report_export_unavailable.html', data, status=409)
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="cost-explorer-report.csv"'
    writer = csv.writer(response)
    writer.writerow([data['group_label'], 'Total (' + data['currency'] + ')', *data['periods']])
    writer.writerow(['Total costs', str(data['total']), *[str(v) if v is not None else '' for v in data['period_totals']]])
    for row in data['pivot_rows']:
        writer.writerow([safe_csv(row['label']), str(row['total']), *[str(v) if v is not None else '' for v in row['cells']]])
    if data.get('comparison_rows'):
        writer.writerow([]); writer.writerow(['Comparison', 'Previous', 'Selected', 'Change', 'Change %'])
        writer.writerow(['Total',data['comparison_total'],data['total'],data['comparison_delta'],data['comparison_percent']])
        for row in data['comparison_rows']:
            writer.writerow([safe_csv(row['label']), row['before'], row['after'], row['delta'], row['percent']])
    if data.get('driver_rows'):
        writer.writerow([]);writer.writerow(['AWS comparison drivers','Customer','Group','Driver','Metric','Unit','Previous','Selected','Change'])
        for row in data['driver_rows']:
            for fact in row['facts']:
                writer.writerow(['Driver',safe_csv(row['customer']),safe_csv(row['label']),safe_csv(fact['name']),safe_csv(fact['metric']),safe_csv(fact['unit']),fact['before'],fact['after'],fact['delta']])
    for note in data.get('driver_notes',[]):writer.writerow(['Comparison driver note',safe_csv(note)])
    if data.get('forecast_rows'):
        writer.writerow([]); writer.writerow(['AWS forecast', 'Customer', 'Start (UTC)', 'End (exclusive UTC)', 'Mean USD', '80% lower USD', '80% upper USD'])
        for row in data['forecast_rows']:
            writer.writerow(['Forecast', safe_csv(row['customer']), row['start'], row['end'], row['mean'], row['lower'], row['upper']])
    writer.writerow([]); writer.writerow(['Report basis', data['metric_label']]); writer.writerow(['Snapshot', data.get('report_snapshot', str(data.get('last_sync', '')))])
    writer.writerow(['Scope', safe_csv(data['scope'].label() if data.get('scope') else 'All customers')])
    for warning in data.get('warnings', []):
        writer.writerow(['Warning', safe_csv(warning)])
    audit(request, 'Grouped cost report exported')
    return response


@never_cache
@login_required
def activity(request):
    return render(request, 'billing/activity.html', {'runs': SyncRun.objects.select_related('customer', 'source')[:100],
        'events': AuditEvent.objects.select_related('customer')[:50], 'jobs': Job.objects.select_related('source', 'source__customer')[:100],
        'queue': scheduler.queue_summary(), 'active_page': 'activity'})


def health(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
    except Exception:
        return JsonResponse({'status': 'unavailable'}, status=503)
    return JsonResponse({'status': 'ok'})


@never_cache
@login_required
def explorer_metadata(request):
    from datetime import date, timedelta
    from django.utils import timezone
    kind = request.GET.get('dimension', '')
    if kind not in contract.FILTERS:
        return JsonResponse({'error': 'Choose a supported billing dimension.'}, status=400)
    key = request.GET.get('key', '')
    if len(key) > 120:
        return JsonResponse({'error': 'Key is too long.'}, status=400)
    today = timezone.now().date()
    try:
        params=request.GET.copy()
        # Choices follow the other filters, never their own current selection. Metadata
        # is independent of incomplete comparison/group/usage controls in the form.
        params.update(group_by='none',group_key='',report_mode='standard',measure='cost',normalized='0',forecast='1',granularity='daily')
        params.setlist(kind,[])
        if kind in ('tag','cost_category'):
            params['untagged' if kind=='tag' else 'uncategorized']='0'
        p=contract.normalize(params)
        start=date.fromisoformat(p['start']);end=min(date.fromisoformat(p['end']),today)
        scope = scoping.resolve(p)
    except (ValueError, TypeError) as exc:
        return JsonResponse({'error': str(exc) or 'Choose valid dates and customer.'}, status=400)
    units = scoping.report_units(scope.customer,start=start,end=end+timedelta(days=1))
    if scope.source:units=[u for u in units if u[0].pk==scope.source.pk]
    if kind == 'resource':
        if start<today-timedelta(days=13):
            return JsonResponse({'error':'Resource values require a date range within the last 14 days and AWS resource-data opt-in.'},status=400)
    req = {'TimePeriod': {'Start': str(start), 'End': str(end + timedelta(days=1))}}
    filters=contract.expression(p)
    if filters:req['Filter']=filters
    if kind in ('tag', 'cost_category'):
        operation = 'get_tags' if kind == 'tag' else 'get_cost_categories'
        if key:
            req['TagKey' if kind == 'tag' else 'CostCategoryName'] = key
        result_key = 'Tags' if kind == 'tag' else 'CostCategoryValues' if key else 'CostCategoryNames'
    else:
        operation = 'get_dimension_values'
        req.update(Dimension=contract.DIMENSIONS[kind][1], Context='COST_AND_USAGE')
        if kind == 'resource':
            service={'Dimensions': {'Key': 'SERVICE', 'Values': [contract.EC2]}}
            req['Filter']={'And':[req['Filter'],service]} if req.get('Filter') else service
        result_key = 'DimensionValues'
    options = set(); labels = {}; errors = []; pending = False; dates = []
    for source, customer, accounts in units:
        for left,right,owned in scoping.ownership_windows(source,customer,start,end+timedelta(days=1)):
            window_req=req|{'TimePeriod':{'Start':str(left),'End':str(right)}}
            try:
                q = get_query(source, operation, window_req, customer=customer, account_filter=owned)
            except ValueError as exc:
                errors.append((customer or source.customer).name + ': ' + str(exc))
                continue
            pending = pending or q.requested
            if q.error:errors.append((customer or source.customer).name + ': ' + q.error)
            if q.last_success:dates.append(q.last_success.isoformat())
            if q.data:
                for v in q.data.get(result_key, []):
                    value = v['Value'] if isinstance(v, dict) else v
                    if kind == 'account' and owned is not None and value not in owned:
                        continue  # metadata obeys the same ownership dates as cost reports
                    options.add(value)
                    if kind=='account' and isinstance(v,dict):
                        name=v.get('Attributes',{}).get('description','')
                        if name:labels[value]=f'{name} ({value})'
    return JsonResponse({'values': sorted(options), 'labels':{v:labels[v] for v in options if v in labels}, 'pending': pending, 'errors': errors, 'snapshots': dates})


@never_cache
@login_required
def explorer_status(request):
    try:
        ids = [int(x) for x in request.GET.get('ids', '').split(',') if x]
    except ValueError:
        return JsonResponse({'error': 'Invalid report IDs.'}, status=400)
    if len(ids) > 500:
        return JsonResponse({'error': 'Too many report IDs.'}, status=400)
    rows = ExplorerQuery.objects.filter(pk__in=ids)
    return JsonResponse({'pending': rows.filter(requested=True).exists(), 'complete': rows.exclude(data=None).count(), 'count': rows.count()})


@require_POST
@staff_required
def save_report(request):
    try:
        p = contract.normalize(request.POST)
        resolved_scope = scoping.resolve(p)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    saved = SavedReport.objects.create(name=p['report_name'], parameters=p, customer=resolved_scope.customer, created_by=request.user.username)
    audit(request, 'Explorer report saved')
    messages.success(request, 'Report saved to your authorized customer library.')
    return redirect('/reports/' + str(saved.pk) + '/')


@login_required
def open_report(request, pk):
    saved = get_object_or_404(SavedReport, pk=pk)
    return redirect('/?' + contract.querydict(saved.parameters).urlencode())


@require_POST
@login_required
def import_report(request):
    try:
        p = contract.import_console_url(request.POST.get('console_url', ''))
    except ValueError as exc:
        return render(request, 'billing/filter_error.html', {'error': str(exc), 'active_page': 'explorer'}, status=400)
    return redirect('/?' + contract.querydict(p).urlencode())
