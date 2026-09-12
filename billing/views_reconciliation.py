from decimal import Decimal
from dataclasses import replace
from functools import wraps
from .access import context, current_access
from urllib.parse import urlencode
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods
from . import alliance
from .models import Customer, Cost
from django.db.models import Sum
from django.db.models.functions import TruncMonth
from dateutil.relativedelta import relativedelta
from .invoice_reconciliation import HEADERS, parse_upload, reconcile, workbook
from .web import audit


def read_scope(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        access = current_access.get()
        with context(replace(access, write=False) if access else None):
            return view(request, *args, **kwargs)
    return wrapped


def download(filename, sheets):
    response = HttpResponse(workbook(sheets), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename="{filename}.xlsx"'
    return response


@never_cache
@login_required
@require_http_methods(['GET', 'POST'])
@read_scope
def reconciliation(request):
    params = request.POST if request.method == 'POST' else request.GET
    try:
        month, currency, threshold, customer = alliance.options(params)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    context = {'active_page': 'reconciliation', 'month': month, 'currency': currency, 'customers': Customer.objects.all(), 'filter_customer': customer,
               'query': urlencode({'month': month.strftime('%Y-%m'), 'currency': currency, 'customer': str(customer.pk) if customer else ''})}
    if request.GET.get('download') == 'template':
        return download('invoice-template', [('Invoice', [HEADERS])])
    if request.method == 'POST':
        try:
            if not customer:
                raise ValueError('Select one customer before reconciling an invoice.')
            if 'invoice' not in request.FILES:
                raise ValueError('Choose a CSV or XLSX invoice file.')
            uploaded = parse_upload(request.FILES['invoice'], month, currency)
            data = alliance.summary(month, currency, threshold, customer)
            context['rows'] = reconcile(data['rows'], uploaded)
            context['matched'] = sum(r['status'] == 'Matched' for r in context['rows'])
            context['differences'] = sum(r['status'] == 'Difference' for r in context['rows'])
            context['incomplete'] = sum(r['status'] == 'AWS data incomplete' for r in context['rows'])
            context['invoice_total'] = sum(uploaded.values(), Decimal(0))
        except ValueError as exc:
            context['error'] = str(exc)
    return render(request, 'billing/reconciliation.html', context)


@never_cache
@login_required
@require_http_methods(['GET'])
def monthly_report(request):
    try:
        month, currency, threshold, customer = alliance.options(request.GET)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    data = alliance.summary(month, currency, threshold, customer)
    executive = alliance.executive_summary(data['rows'], threshold)
    summary = [['Monthly billing report', month.strftime('%Y-%m')], ['Currency', currency], ['Metric', 'Unblended AWS cost'],
               ['Scope', customer.name if customer else 'All authorized customers/accounts'],
               ['Comparison', 'Changes appear only when both months are complete. Blank is unavailable, not zero.'],
               ['Invoice reconciliation', 'AWS cost excludes any manual reseller margin or invoice adjustments.'],
               ['Customer', 'Accounts', 'Current available spend', 'Prior available spend', 'Change', 'Change %', 'Accounts needing data checks']]
    for group in executive['groups']:
        summary.append([group['customer'].name, group['count'], group['current'], group['prior'], group['comparison']['delta'], group['comparison']['percent'], group['data_count']])
    accounts = [['Customer', 'Account', 'AWS account ID', 'Currency', 'Billing month', 'Current spend', 'Previous spend', 'Change', 'Change %', 'Current data', 'Previous data', 'Review']]
    for row in executive['accounts']:
        accounts.append([row['customer'].name, row['name'], row['account_id'], currency, month.strftime('%Y-%m'), row['current']['value'], row['prior']['value'], row['comparison']['delta'], row['comparison']['percent'], row['current']['state'], row['prior']['state'], row['signal']])
    services = [['Customer', 'AWS account ID', 'Service', 'Current spend', 'Previous spend', 'Change', 'Explanation']]
    facts = Cost.objects.filter(customer__isnull=False, currency=currency, day__gte=data['prior_month'], day__lt=month+relativedelta(months=1))
    if customer:
        facts = facts.filter(customer=customer)
    totals = {}
    for item in facts.annotate(period=TruncMonth('day')).values('customer_id', 'account_id', 'service', 'period').annotate(value=Sum('unblended')):
        totals.setdefault((item['customer_id'], item['account_id'], item['service']), {})[item['period']] = item['value']
    account_map = {(r['customer'].pk, r['account_id']): r for r in executive['accounts']}
    for (cid, account, service), amounts in sorted(totals.items(), key=lambda item: str(item[0])):
        row = account_map.get((cid, account))
        if not row:
            continue
        complete = row['current']['state'] == row['prior']['state'] == 'Complete'
        current = amounts.get(month, Decimal(0) if row['current']['state'] == 'Complete' else None)
        prior = amounts.get(data['prior_month'], Decimal(0) if row['prior']['state'] == 'Complete' else None)
        delta = current-prior if complete else None
        explanation = ('Increase in service cost' if delta > 0 else 'Decrease in service cost' if delta < 0 else 'No change') if delta is not None else 'Comparison unavailable: incomplete or estimated month'
        services.append([row['customer'].name, account, service, current, prior, delta, explanation])
    audit(request, 'Monthly Excel report exported', customer=customer, month=str(month), currency=currency)
    return download('monthly-billing-'+month.strftime('%Y-%m'), [('Executive summary', summary), ('Account details', accounts), ('Service changes', services)])
