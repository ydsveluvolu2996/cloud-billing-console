import csv
from functools import wraps
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import connection
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from django.views.decorators.cache import never_cache
from .collector import verify
from .forms import BudgetForm, ConnectionForm, CustomerForm
from .models import AuditEvent, Cost, Customer, SyncRun, ExplorerQuery, SavedReport
from .advanced_explorer import build_report
from . import parameters as contract
from .query_cache import get_query
from .onboarding import customer_template, quick_create_url
from .reporting import report
from .explorer import defaults as explorer_defaults, explorer_report


def staff_required(view):
    @wraps(view)
    @login_required
    def wrapped(request, *args, **kwargs):
        if not request.user.is_staff:
            return HttpResponseForbidden('An administrator must perform this action.')
        return view(request, *args, **kwargs)
    return wrapped


@never_cache
@login_required
def dashboard(request):
    try:
        return render(request, 'billing/explorer.html', build_report(request.GET))
    except ValueError as exc:
        return render(request, 'billing/filter_error.html', {'error':str(exc), 'active_page':'explorer'}, status=400)


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
        return render(request, 'billing/filter_error.html', {'error': str(exc), 'active_page': 'overview'}, status=400)
    choices = Cost.objects.all()
    if context['filter_customer']:
        choices = choices.filter(customer_id=context['filter_customer'])
    context.update({'customers': Customer.objects.all(),
        'currencies': sorted(set(Cost.objects.values_list('currency', flat=True)) | {'USD'}),
        'accounts': choices.order_by('account_id').values_list('account_id', flat=True).distinct(),
        'service_options': choices.order_by('service').values_list('service', flat=True).distinct(),
        'active_page': 'explorer' if explorer else 'overview'})
    return render(request, 'billing/explorer.html' if explorer else 'billing/dashboard.html', context)


@never_cache
@login_required
def customers(request):
    items = list(Customer.objects.all())
    return render(request, 'billing/customers.html', {'customers': items, 'active_page': 'customers',
        'connected_count': sum(c.status == 'Connected' for c in items),
        'attention_count': sum(c.status in ('Stale', 'Action required', 'Awaiting setup') for c in items),
        'paused_count': sum(not c.enabled for c in items)})


@staff_required
def customer_add(request):
    form = CustomerForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        customer = form.save()
        AuditEvent.objects.create(actor=request.user.username, action='Customer added', customer=customer)
        return redirect('customer_detail', pk=customer.pk)
    return render(request, 'billing/customer_add.html', {'form': form, 'active_page': 'customers'})


@never_cache
@staff_required
def customer_detail(request, pk):
    customer = get_object_or_404(Customer, pk=pk)
    form = ConnectionForm(instance=customer)
    budget_form = BudgetForm(instance=customer)
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'connection':
            form = ConnectionForm(request.POST, instance=customer)
            if form.is_valid():
                customer = form.save()
                if verify(customer):
                    customer.sync_requested = True
                    customer.save(update_fields=['sync_requested'])
                    messages.success(request, 'Connection verified. The first import is queued and starts within a minute.')
                else:
                    messages.error(request, customer.last_error)
                AuditEvent.objects.create(actor=request.user.username, action='Connection verified' if not customer.last_error else 'Verification failed', customer=customer)
                return redirect('customer_detail', pk=pk)
        elif action == 'budget':
            budget_form = BudgetForm(request.POST, instance=customer)
            if budget_form.is_valid():
                budget_form.save()
                AuditEvent.objects.create(actor=request.user.username, action='Budget updated', customer=customer)
                messages.success(request, 'Budget updated.')
                return redirect('customer_detail', pk=pk)
        elif action == 'toggle':
            customer.enabled = not customer.enabled
            customer.save(update_fields=['enabled'])
            AuditEvent.objects.create(actor=request.user.username, action='Collection resumed' if customer.enabled else 'Collection paused', customer=customer)
            return redirect('customer_detail', pk=pk)
    return render(request, 'billing/customer_detail.html', {'customer': customer, 'form': form, 'budget_form': budget_form,
        'runs': customer.syncs.all()[:10], 'active_page': 'customers'})


@staff_required
def template_download(request, pk):
    customer = get_object_or_404(Customer, pk=pk)
    try:
        body = customer_template(customer)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    response = HttpResponse(body, content_type='application/x-yaml')
    response['Content-Disposition'] = 'attachment; filename="customer-billing-role.yaml"'
    return response


@staff_required
def launch_setup(request, pk):
    customer = get_object_or_404(Customer, pk=pk)
    try:
        url = quick_create_url(customer)
    except Exception:
        url = ''
    if not url:
        messages.error(request, 'Setup link is unavailable. Download the template and upload it in AWS CloudFormation.')
        return redirect('customer_detail', pk=pk)
    return render(request, 'billing/setup_link.html', {'customer': customer, 'setup_url': url, 'active_page': 'customers'})


@require_POST
@staff_required
def request_sync(request, pk):
    customer = get_object_or_404(Customer, pk=pk)
    if customer.enabled and customer.role_arn:
        Customer.objects.filter(pk=pk).update(sync_requested=True)
        AuditEvent.objects.create(actor=request.user.username, action='Sync requested', customer=customer)
        messages.success(request, 'Sync queued. Collection starts within a minute; large imports may take longer.')
    else:
        messages.error(request, 'Complete onboarding and enable collection first.')
    return redirect('customer_detail', pk=pk)


@require_POST
@staff_required
def refresh_costs(request):
    # Reuse validated filters; never redirect to an arbitrary submitted URL.
    try:
        data = build_report(request.POST) if request.POST.get('group_by') else report(request.POST)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    items = Customer.objects.filter(enabled=True).exclude(role_arn='')
    if data['filter_customer']:
        items = items.filter(pk=data['filter_customer'])
    count = items.update(sync_requested=True)
    ExplorerQuery.objects.filter(customer__in=items, pk__in=data.get('query_ids', [])).update(requested=True)
    if count:
        AuditEvent.objects.create(actor=request.user.username, action=f'Billing refresh requested for {count} customer(s)')
        messages.success(request, f'Refresh queued for {count} customer(s). Collection starts within a minute. Reload the page after the import completes.')
    else:
        messages.error(request, 'Connect an active customer before requesting a refresh.')
    return redirect('/?' + data['export_query'])


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
    writer.writerow(['Date (UTC)', 'Customer', 'AWS account', 'Service', 'Currency', 'Unblended cost', 'Amortized cost', 'Estimated'])
    for cost in data['selected'].select_related('customer').order_by('day', 'customer__name', 'service').iterator():
        writer.writerow([cost.day.isoformat(), safe_csv(cost.customer.name), cost.account_id, safe_csv(cost.service),
                         cost.currency, str(cost.unblended), str(cost.amortized), cost.estimated])
    AuditEvent.objects.create(actor=request.user.username, action='Cost report exported')
    return response


@never_cache
@login_required
def export_report(request):
    params = explorer_defaults(request.GET)
    try:
        data = build_report(request.GET)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    if data.get('report_incomplete'):
        return HttpResponse('Report is incomplete. Wait for all customer queries to finish before exporting.', status=409)
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="cost-explorer-report.csv"'
    writer = csv.writer(response)
    writer.writerow([data['group_label'], 'Total (' + data['currency'] + ')', *data['periods']])
    writer.writerow(['Total costs', str(data['total']), *[str(v) if v is not None else '' for v in data['period_totals']]])
    for row in data['pivot_rows']:
        writer.writerow([safe_csv(row['label']), str(row['total']), *[str(v) if v is not None else '' for v in row['cells']]])
    if data.get('comparison_rows'):
        writer.writerow([]); writer.writerow(['Comparison', 'Previous', 'Selected', 'Change', 'Change %'])
        for row in data['comparison_rows']:
            writer.writerow([safe_csv(row['label']),row['before'],row['after'],row['delta'],row['percent']])
    if data.get('forecast_rows'):
        writer.writerow([]); writer.writerow(['AWS forecast', 'Customer', 'Start (UTC)', 'End (exclusive UTC)', 'Mean USD', '80% lower USD', '80% upper USD'])
        for row in data['forecast_rows']:
            writer.writerow(['Forecast',safe_csv(row['customer']),row['start'],row['end'],row['mean'],row['lower'],row['upper']])
    writer.writerow([]); writer.writerow(['Report basis', data['metric_label']]);writer.writerow(['Snapshot', data.get('report_snapshot', str(data.get('last_sync','')))])
    for warning in data.get('warnings',[]):writer.writerow(['Warning',safe_csv(warning)])
    AuditEvent.objects.create(actor=request.user.username, action='Grouped cost report exported')
    return response


@never_cache
@login_required
def activity(request):
    return render(request, 'billing/activity.html', {'runs': SyncRun.objects.select_related('customer')[:100],
        'events': AuditEvent.objects.select_related('customer')[:50], 'active_page': 'activity'})


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
    kind=request.GET.get('dimension','')
    if kind not in contract.FILTERS:
        return JsonResponse({'error':'Choose a supported billing dimension.'},status=400)
    key=request.GET.get('key','')
    if len(key)>120:return JsonResponse({'error':'Key is too long.'},status=400)
    today=timezone.now().date()
    try:
        start=date.fromisoformat(request.GET.get('start',str(today.replace(day=1))))
        end=min(date.fromisoformat(request.GET.get('end',str(today))),today)
        if start>end or (end-start).days>731:raise ValueError()
        customers=Customer.objects.filter(enabled=True,last_success__isnull=False).exclude(role_arn='')
        if request.GET.get('customer'):
            from uuid import UUID
            customers=customers.filter(pk=UUID(request.GET['customer']))
    except (ValueError,TypeError):return JsonResponse({'error':'Choose valid dates and customer.'},status=400)
    if kind=='resource':start=max(start,today-timedelta(days=13))
    req={'TimePeriod':{'Start':str(start),'End':str(end+timedelta(days=1))}}
    if kind in ('tag','cost_category'):
        operation='get_tags' if kind=='tag' else 'get_cost_categories'
        if key:req['TagKey' if kind=='tag' else 'CostCategoryName']=key
        result_key='Tags' if kind=='tag' else 'CostCategoryValues' if key else 'CostCategoryNames'
    else:
        operation='get_dimension_values';req.update(Dimension=contract.DIMENSIONS[kind][1],Context='COST_AND_USAGE')
        if kind=='resource':req['Filter']={'Dimensions':{'Key':'SERVICE','Values':[contract.EC2]}}
        result_key='DimensionValues'
    options=set(); errors=[]; pending=False; dates=[]
    for customer in customers:
        q=get_query(customer,operation,req);pending=pending or q.requested
        if q.error:errors.append(customer.name+': '+q.error)
        if q.last_success:dates.append(q.last_success.isoformat())
        if q.data:
            for v in q.data.get(result_key,[]):options.add(v['Value'] if isinstance(v,dict) else v)
    return JsonResponse({'values':sorted(options),'pending':pending,'errors':errors,'snapshots':dates})


@never_cache
@login_required
def explorer_status(request):
    try:ids=[int(x) for x in request.GET.get('ids','').split(',') if x]
    except ValueError:return JsonResponse({'error':'Invalid report IDs.'},status=400)
    if len(ids)>500:return JsonResponse({'error':'Too many report IDs.'},status=400)
    rows=ExplorerQuery.objects.filter(pk__in=ids)
    return JsonResponse({'pending':rows.filter(requested=True).exists(), 'complete':rows.exclude(data=None).count(), 'count':rows.count()})


@require_POST
@staff_required
def save_report(request):
    try:p=contract.normalize(request.POST)
    except ValueError as exc:return HttpResponseBadRequest(str(exc))
    saved=SavedReport.objects.create(name=p['report_name'],parameters=p,created_by=request.user.username)
    AuditEvent.objects.create(actor=request.user.username,action='Explorer report saved')
    messages.success(request,'Report saved to the shared library.')
    return redirect('/reports/'+str(saved.pk)+'/')


@login_required
def open_report(request,pk):
    saved=get_object_or_404(SavedReport,pk=pk)
    return redirect('/?'+contract.querydict(saved.parameters).urlencode())


@require_POST
@login_required
def import_report(request):
    try:p=contract.import_console_url(request.POST.get('console_url',''))
    except ValueError as exc:return render(request,'billing/filter_error.html',{'error':str(exc),'active_page':'explorer'},status=400)
    return redirect('/?'+contract.querydict(p).urlencode())
