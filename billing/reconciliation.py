"""Decimal-safe reconciliation of an explicitly described customer reference export."""
import csv
import io
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from django.db.models import Sum
from .models import Cost, AccountAssignment, CollectionPeriod
from django.db.models import Q

REQUIRED = ('account_id','amount','currency','metric','start','end','timezone','estimated')


def reconcile(customer, reference_text, start, end, metric, currency, source=None, services=None, tolerance=Decimal('0.00000001')):
    if metric not in ('unblended','amortized') or start>end:
        raise ValueError('Choose a supported cost metric and inclusive date range.')
    if len(currency)!=3 or not currency.isupper():
        raise ValueError('Choose one currency; mixed currency comparison is not supported.')
    services=sorted(set(services or []))
    reader=csv.DictReader(io.StringIO(reference_text))
    if not reader.fieldnames or not set(REQUIRED).issubset(reader.fieldnames):
        raise ValueError('Reference CSV is missing required scope columns: '+', '.join(REQUIRED))
    reference={};issues=[]
    for line,row in enumerate(reader,2):
        account=row['account_id']
        if account in reference:raise ValueError(f'Line {line}: duplicate account reference total.')
        if any(row[k]!=v for k,v in {'currency':currency,'metric':metric,'start':start.isoformat(),'end':end.isoformat(),'timezone':'UTC'}.items()):
            raise ValueError(f'Line {line}: dates, metric, currency or timezone do not match.')
        if sorted(filter(None,row.get('services','').split('|'))) != services:
            raise ValueError(f'Line {line}: service filter differs from the dashboard scope.')
        if row.get('credits_refunds','included')!='included':
            raise ValueError(f'Line {line}: reference must include credits and refunds.')
        if row['estimated'].lower() not in ('true','false'):
            raise ValueError(f'Line {line}: estimated must be true or false.')
        try:
            amount=Decimal(row['amount'])
            if not amount.is_finite():raise InvalidOperation
        except InvalidOperation:raise ValueError(f'Line {line}: invalid decimal amount.') from None
        reference[account]={'amount':amount,'estimated':row['estimated'].lower()=='true'}
    assignments=AccountAssignment.objects.filter(customer=customer,start__lte=end).filter(Q(end__isnull=True)|Q(end__gt=start))
    if source:assignments=assignments.filter(account__source=source)
    owned=set(assignments.values_list('account__account_id',flat=True))
    unexpected=set(reference)-owned
    if unexpected:raise ValueError('Reference contains accounts outside this customer\'s historical assigned scope.')
    qs=Cost.objects.filter(customer=customer,day__gte=start,day__lte=end,currency=currency)
    if source:qs=qs.filter(source=source)
    if services:qs=qs.filter(service__in=services)
    totals={r['account_id']:r['amount'] for r in qs.values('account_id').annotate(amount=Sum(metric))}
    estimated=set(qs.filter(estimated=True).values_list('account_id',flat=True))
    rows=[]
    for account in sorted(owned|set(totals)):
        actual=totals.get(account)
        expected=reference.get(account)
        account_sources=set(assignments.filter(account__account_id=account).values_list('account__source_id',flat=True))
        months=[];cursor=start.replace(day=1)
        from dateutil.relativedelta import relativedelta
        while cursor<=end:
            months.append(cursor);cursor+=relativedelta(months=1)
        periods=list(CollectionPeriod.objects.filter(source_id__in=account_sources,month__in=months))
        complete=len(periods)==len(account_sources)*len(months) and bool(account_sources) and all(p.status=='complete' and p.last_success and p.first_day and p.last_day and p.first_day<=max(start,p.month) and p.last_day>=min(end,p.month+relativedelta(months=1)-timedelta(days=1)) for p in periods)
        # Zero requires both published coverage and evidence of the selected currency.
        if actual is None and complete and Cost.objects.filter(source_id__in=account_sources,currency=currency,day__gte=start,day__lte=end).exists():actual=Decimal(0)
        delta=actual-expected['amount'] if actual is not None and expected else None
        state='matched' if delta is not None and abs(delta)<=tolerance else 'difference' if delta is not None else 'missing reference' if not expected else 'missing data'
        is_estimated=account in estimated or (account not in totals and any(p.estimated for p in periods))
        if expected and expected['estimated']!=is_estimated:state='estimate status differs'
        if not complete and state=='matched':state='incomplete coverage'
        rows.append({'account_id':account,'dashboard':str(actual) if actual is not None else None,'reference':str(expected['amount']) if expected else None,'difference':str(delta) if delta is not None else None,'state':state,'estimated':is_estimated,'ownership_windows':[{'start':str(max(start,a.start)),'end_exclusive':str(min(end+timedelta(days=1),a.end or end+timedelta(days=1)))} for a in assignments.filter(account__account_id=account)]})
    from .governance import configuration
    return {'configuration':configuration(customer),'passed':bool(rows) and all(r['state']=='matched' for r in rows),'customer':str(customer.pk),'source':str(source.pk) if source else None,
            'start':str(start),'end_inclusive':str(end),'aws_end_exclusive':str(end+timedelta(days=1)),
            'metric':metric,'currency':currency,'timezone':'UTC','services':services,'credits_refunds':'included','accounts':rows,'tolerance':str(tolerance)}
