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
from .models import AuditEvent, Cost, Customer, SyncRun
from .onboarding import customer_template, quick_create_url
from .reporting import report


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
        context = report(request.GET)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    context.update({'customers': Customer.objects.all(),
        'currencies': sorted(set(Cost.objects.values_list('currency', flat=True)) | {'USD'}),
        'accounts': Cost.objects.order_by('account_id').values_list('account_id', flat=True).distinct(),
        'service_options': Cost.objects.order_by('service').values_list('service', flat=True).distinct(),
        'stale_count': sum(c.status in ('Stale', 'Action required') for c in Customer.objects.all()),
        'latest_sync': SyncRun.objects.filter(status='success').first(), 'active_page': 'overview'})
    return render(request, 'billing/dashboard.html', context)


@never_cache
@login_required
def customers(request):
    return render(request, 'billing/customers.html', {'customers': Customer.objects.all(), 'active_page': 'customers'})


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
