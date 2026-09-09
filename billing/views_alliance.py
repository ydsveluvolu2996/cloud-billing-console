import csv
from decimal import Decimal
from urllib.parse import urlencode
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods
from . import alliance
from .forms_alliance import AllianceForm
from .models import AllianceRecord, AllianceServiceNote, AwsAccount, BillingSource, Cost, Customer
from .scope import can_edit
from .web import audit, paginate, safe_csv


def csv_response(filename, headers, rows):
    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="{filename}.csv"'
    writer = csv.writer(response)
    writer.writerow(headers)
    for row in rows:
        writer.writerow(['' if value is None else safe_csv(value) if isinstance(value,str) else value for value in row])
    return response


@never_cache
@login_required
@require_http_methods(['GET'])
def overview(request):
    try:
        month, currency, threshold, customer = alliance.options(request.GET)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    data = alliance.summary(month,currency,threshold,customer)
    rows = data['rows']
    search = request.GET.get('q','').strip()[:200]
    if search:
        rows = [r for r in rows if search.lower() in ' '.join((r['name'],r['customer'].name,r['account_id'],r['legal_entity'],r['ace'])).lower()]
    status = request.GET.get('status','')
    if status == 'review':
        rows = [r for r in rows if r['flag'] == 'REVIEW' or r['revised']]
    elif status == 'open':
        rows = [r for r in rows if r['status'] != 'Closed']
    elif status == 'closed':
        rows = [r for r in rows if r['status'] == 'Closed']
    elif status == 'missing':
        rows = [r for r in rows if r['missing_ids']]
    tab = 'handoff' if request.GET.get('tab') == 'handoff' else 'summary'
    if request.GET.get('export') == 'csv':
        if tab == 'summary':
            headers = ['S.No','Account','Customer Legal Entity (as in Partner Central)','AWS Account ID','ACE Opportunity ID',
                       *[m.strftime('%b-%y') for m in data['months']], 'FY Total','Avg / Month','Reporting Mth','Prior Mth','MoM Var','MoM Var %','Flag',
                       'Customer','Currency','Reporting Month','Prior Month','Reporting data state','Prior data state','Months with data','Variance threshold %',
                       *[m.strftime('%b-%y')+' data state' for m in data['months']]]
            values = [[i,r['name'],r['legal_entity'],r['account_id'],r['ace'],*[c['value'] for c in r['cells']],r['fy_total'],r['average'],r['current']['value'],r['prior']['value'],r['delta'],r['percent'],r['flag'],
                       r['customer'].name,currency,month,data['prior_month'],r['current']['state'],r['prior']['state'],r['months_loaded'],threshold,*[c['state'] for c in r['cells']]] for i,r in enumerate(rows,1)]
        else:
            headers = ['Reporting Month','Account','AWS Account ID','ACE Opportunity ID','Bill Pulled On','Prepared By','Shared With (Alliance)','Date Shared','Spend ('+currency+')','MoM Var %','APN Marked (Y/N)','APN Marked Date','Notes / Blocker','Status','Customer','Currency','Recorded data state','Captured at (UTC)']
            values = []
            for r in rows:
                record = r['record']
                values.append([month,r['name'],r['account_id'],r['ace'],record.bill_pulled_on if record else None,record.prepared_by if record else '',record.shared_with if record else '',record.date_shared if record else None,
                               r['recorded_spend'],r['recorded_percent'],'Y' if record and record.apn_marked else 'N',record.apn_marked_date if record else None,record.notes if record else '',r['status'],r['customer'].name,currency,
                               record.snapshot.get('state','') if record else '',record.snapshot.get('captured_at','') if record else ''])
        audit(request,'Alliance report exported',customer=customer,month=str(month),tab=tab,currency=currency)
        return csv_response('alliance-'+tab+'-'+month.strftime('%Y-%m'),headers,values)
    params = {'month':month.strftime('%Y-%m'),'currency':currency,'threshold':str(threshold)}
    if customer:
        params['customer'] = str(customer.pk)
    params.update({'q':search,'status':status})
    query = urlencode(params)
    context = {**data,'page':paginate(request,rows),'row_count':len(rows),'month':month,'currency':currency,'threshold':threshold,
               'filter_customer':customer,'customers':Customer.objects.all(),'currencies':sorted(set(Cost.objects.values_list('currency',flat=True))|{'USD',currency}),
               'q':search,'filter_status':status,'tab':tab,'query':query,'detail_query':urlencode({k:params[k] for k in ('month','currency','threshold')}),
               'query_string':query+'&tab='+tab,'active_page':'alliance','can_edit':can_edit(request.user),
               'spend':alliance.available_sum([r['current']['value'] for r in rows]),
               'review_count':sum(r['flag']=='REVIEW' or r['revised'] for r in rows),
               'closed_count':sum(r['status']=='Closed' for r in rows),
               'incomplete_count':sum(r['current']['state']!='Complete' for r in rows),
               'missing_count':sum(r['missing_ids'] for r in rows)}
    return render(request,'billing/alliance.html',context)


@never_cache
@login_required
@require_http_methods(['GET','POST'])
def detail(request, customer_id, account_id):
    customer = get_object_or_404(Customer,pk=customer_id)
    account = get_object_or_404(AwsAccount,account_id=account_id)
    try:
        month,currency,threshold,_ = alliance.options(request.GET)
        data = alliance.account_detail(customer,account,month,currency,threshold)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    record = data['record']
    if not record:
        previous = AllianceRecord.objects.filter(customer=customer,account=account,month__lt=month,currency=currency).first()
        record = AllianceRecord(customer=customer,account=account,month=month,currency=currency,
            account_name=previous.account_name if previous else account.name or customer.name,
            legal_entity=previous.legal_entity if previous else '',ace_opportunity_id=previous.ace_opportunity_id if previous else '')
    fresh = alliance.snapshot(data,threshold)
    revised = bool(record.snapshot) and record.snapshot.get('fingerprint') != fresh['fingerprint']
    form = AllianceForm(instance=record,services=[r['service'] for r in data['services']],captured=record.snapshot,revised=revised)
    response_status = 200
    if request.method == 'POST':
        if not can_edit(request.user):
            return HttpResponseForbidden('An operator or administrator must update alliance tracking.')
        action = request.POST.get('action','save')
        if action not in ('save','capture'):
            return HttpResponseBadRequest('Choose a valid tracking action.')
        with transaction.atomic():
            # Serialize creation as well as edits; collector publication holds the
            # same source lock so a captured snapshot cannot mix two imports.
            Customer.objects.select_for_update().get(pk=customer.pk)
            source_ids = set(Cost.objects.filter(customer=customer,account_id=account_id).values_list('source_id',flat=True))
            if account.source_id:
                source_ids.add(account.source_id)
            list(BillingSource.objects.select_for_update().filter(pk__in=source_ids).order_by('pk'))
            locked = AllianceRecord.objects.select_for_update().filter(customer=customer,account=account,month=month,currency=currency).first()
            record = locked or record
            was_closed = record.apn_marked
            data = alliance.account_detail(customer,account,month,currency,threshold)
            fresh = alliance.snapshot(data,threshold)
            revised = bool(record.snapshot) and record.snapshot.get('fingerprint') != fresh['fingerprint']
            candidate = fresh if action == 'capture' else record.snapshot
            form = AllianceForm(request.POST,instance=record,services=[r['service'] for r in data['services']],captured=candidate,revised=False if action=='capture' else revised)
            valid = form.is_valid()
            if form.cleaned_data.get('version') != record.revision:
                form.add_error(None,'Someone updated this record while you were editing. Reload the page before saving. Your changes have not been saved.')
                valid, response_status = False,409
            if action == 'capture' and (was_closed or form.cleaned_data.get('apn_marked')):
                form.add_error(None,'Reopen the entry by clearing APN marked and its date, then save before replacing the recorded figures.')
                valid = False
            if action == 'capture' and data['current']['value'] is None:
                form.add_error(None,'No AWS spend is available to record for this month and currency.')
                valid = False
            if valid:
                saved = form.save(commit=False)
                if action == 'capture':
                    fresh['captured_at'] = timezone.now().isoformat()
                    fresh['captured_by'] = request.user.username
                    fresh['last_import'] = data['current'].get('last_import').isoformat() if data['current'].get('last_import') else None
                    saved.snapshot = fresh
                    if not saved.bill_pulled_on:
                        saved.bill_pulled_on = timezone.now().date()
                saved.updated_by = request.user.username
                saved.revision += 1
                saved.save()
                for service,name in form.service_fields:
                    AllianceServiceNote.objects.update_or_create(record=saved,service=service,defaults={'commentary':form.cleaned_data[name]})
                audit(request,'Alliance figures recorded' if action=='capture' else 'Alliance tracking updated',customer=customer,
                      account=account_id,month=str(month),currency=currency,revision=saved.revision,apn_marked=saved.apn_marked,
                      snapshot=saved.snapshot,fields={name:str(form.cleaned_data.get(name,'')) for name in form.Meta.fields},
                      service_notes={service:form.cleaned_data[name] for service,name in form.service_fields})
                messages.success(request,'AWS figures recorded. Handoff history is preserved in the audit log.' if action=='capture' else 'Alliance tracking saved.')
                return redirect(reverse('alliance_detail',args=[customer.pk,account_id])+'?'+urlencode({'month':month.strftime('%Y-%m'),'currency':currency,'threshold':str(threshold)}))
    notes = dict(record.service_notes.values_list('service','commentary')) if record.pk else {}
    for row,(_,name) in zip(data['services'],form.service_fields):
        row['field'],row['commentary'] = form[name],notes.get(row['service'],'')
    if request.method == 'GET' and request.GET.get('export') == 'csv':
        headers = ['Service','Reporting Mth ('+currency+')','Prior Mth ('+currency+')','Var ('+currency+')','Var %','Flag','Variance Driver / Internal Commentary',
                   'Account Name','Customer Legal Entity','AWS Account ID','ACE Opportunity ID','Reporting Month','Prior Month','Prepared By','Date Shared with Alliance','Summary note for alliance team','Reporting data state','Prior data state','Currency']
        values = [[r['service'],r['current'],r['prior'],r['delta'],r['percent'],r['flag'],r['commentary'],record.account_name,record.legal_entity,account_id,record.ace_opportunity_id,
                   month,month-alliance.relativedelta(months=1),record.prepared_by,record.date_shared,record.summary_note,data['current']['state'],data['prior']['state'],currency] for r in data['services']]
        if not values:
            values = [['',None,None,None,None,'No comparison','',record.account_name,record.legal_entity,account_id,record.ace_opportunity_id,month,month-alliance.relativedelta(months=1),record.prepared_by,record.date_shared,record.summary_note,data['current']['state'],data['prior']['state'],currency]]
        audit(request,'Alliance account detail exported',customer=customer,account=account_id,month=str(month),currency=currency)
        return csv_response('alliance-account-'+account_id+'-'+month.strftime('%Y-%m'),headers,values)
    return render(request,'billing/alliance_detail.html',{**data,'record':record,'form':form,'metadata_fields':[form[name] for name in form.Meta.fields],
        'customer':customer,'account':account,'month':month,'prior_month':month-alliance.relativedelta(months=1),'currency':currency,'threshold':threshold,
        'query':urlencode({'month':month.strftime('%Y-%m'),'currency':currency,'threshold':str(threshold)}),
        'recorded_spend':Decimal(record.snapshot['current']) if record.snapshot.get('current') is not None else None,
        'recorded_percent':Decimal(record.snapshot['percent']) if record.snapshot.get('percent') is not None else None,
        'captured_at':parse_datetime(record.snapshot['captured_at']) if record.snapshot.get('captured_at') else None,
        'captured_import':parse_datetime(record.snapshot['last_import']) if record.snapshot.get('last_import') else None,
        'revised':revised,'active_page':'alliance','can_edit':can_edit(request.user)},status=response_status)
