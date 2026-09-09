"""Read-only AWS spend projections for the MSP alliance tracking workbook.

Facts retain effective-dated customer ownership. Missing coverage never becomes zero.
Handoff snapshots are separate from the AWS facts and survive later cost revisions.
"""
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from hashlib import sha256
import json
from dateutil.relativedelta import relativedelta
from django.db.models import Q, Sum, Max, Case, When, IntegerField, OuterRef, Subquery
from django.db.models.functions import TruncMonth
from django.utils import timezone
from .models import AccountAssignment, AllianceRecord, AwsAccount, CollectionPeriod, Cost, Customer


def month_start(value=None):
    if value:
        try:
            return date.fromisoformat(str(value) + ('-01' if len(str(value)) == 7 else '')).replace(day=1)
        except (ValueError, TypeError):
            raise ValueError('Choose a valid reporting month.')
    return timezone.now().date().replace(day=1) - relativedelta(months=1)


def options(params):
    month = month_start(params.get('month'))
    if not 2000 <= month.year <= 2100:
        raise ValueError('Choose a reporting month between 2000 and 2100.')
    currency = params.get('currency', 'USD')
    if len(currency) != 3 or not currency.isalpha() or not currency.isupper():
        raise ValueError('Choose a three-letter currency code, such as USD.')
    try:
        threshold = Decimal(params.get('threshold', '15'))
        if not threshold.is_finite() or not 0 <= threshold <= 1000:
            raise ValueError
    except (ValueError, ArithmeticError):
        raise ValueError('Variance threshold must be between 0 and 1,000 percent.')
    customer = None
    if params.get('customer'):
        from .scope import parse_uuid
        try:
            customer = Customer.objects.get(pk=parse_uuid(params['customer'], 'Choose a valid customer.'))
        except Customer.DoesNotExist:
            raise ValueError('Choose a valid customer.')
    return month, currency, threshold, customer


def variance(current, prior, threshold=Decimal('15')):
    if current is None or prior is None:
        return {'delta': None, 'percent': None, 'flag': 'No comparison'}
    delta = current - prior
    if prior == 0:
        return {'delta': delta, 'percent': Decimal(0) if current == 0 else None,
                'flag': 'OK' if current == 0 else 'REVIEW', 'zero_baseline': current != 0}
    percent = delta / prior * 100
    return {'delta': delta, 'percent': percent, 'flag': 'REVIEW' if abs(percent) >= threshold else 'OK'}


def available_sum(values):
    present = [value for value in values if value is not None]
    return sum(present, Decimal(0)) if present else None


class SpendData:
    def __init__(self, first, last, currency, customer=None):
        self.first, self.last, self.currency = first, last, currency
        self.today = timezone.now().date()
        costs = Cost.objects.filter(customer__isnull=False, day__gte=first, day__lt=last, currency=currency)
        assignments = AccountAssignment.objects.filter(start__lt=min(last, self.today + timedelta(days=1))).filter(Q(end__isnull=True) | Q(end__gt=first))
        if customer:
            costs = costs.filter(customer=customer)
            assignments = assignments.filter(customer=customer)
        self.costs = costs
        self.service_amounts, self.services_loaded = {}, set()
        self.assignments = defaultdict(list)
        self.accounts, self.customers, self.facts = {}, {}, {}
        for item in assignments.select_related('account', 'customer'):
            key = (item.customer_id, item.account.account_id)
            self.assignments[key].append(item)
            self.accounts[item.account.account_id] = item.account
            self.customers[item.customer_id] = item.customer
        groups = costs.annotate(period=TruncMonth('day')).values('customer_id','account_id','source_id','period').annotate(amount=Sum('unblended'), estimated=Max(Case(When(estimated=True,then=1),default=0,output_field=IntegerField())))
        self.facts = defaultdict(list)
        for item in groups:
            key = (item['customer_id'], item['account_id'])
            self.facts[(key, item['period'])].append(item)
        missing_accounts = {k[0][1] for k in self.facts} - self.accounts.keys()
        self.accounts.update({a.account_id:a for a in AwsAccount.objects.filter(account_id__in=missing_accounts)})
        missing_customers = {k[0][0] for k in self.facts} - self.customers.keys()
        self.customers.update(Customer.objects.in_bulk(missing_customers))
        self.keys = set(self.assignments) | {k[0] for k in self.facts}
        self.periods = {(p.source_id,p.month):p for p in CollectionPeriod.objects.filter(month__gte=first,month__lt=last)}
        # Currency must be evidenced before an absent account row can mean a zero.
        self.currency_periods = set(Cost.objects.filter(day__gte=first,day__lt=last,currency=currency).annotate(period=TruncMonth('day')).values_list('source_id','period').distinct())

    def cell(self, key, month):
        end = month + relativedelta(months=1)
        if month > self.today:
            return {'value':None, 'state':'Future', 'estimated':False}
        facts = self.facts.get((key, month), [])
        account = self.accounts.get(key[1])
        owned = any(a.start < end and (a.end is None or a.end > month) for a in self.assignments.get(key, []))
        if not facts and not owned:
            return {'value':None, 'state':'Not owned', 'estimated':False}
        sources = {f['source_id'] for f in facts} or ({account.source_id} if account and account.source_id else set())
        periods = [self.periods.get((s,month)) for s in sources]
        expected_end = min(end - timedelta(days=1), self.today)
        covered = bool(periods) and all(p and p.status == 'complete' and p.first_day and p.first_day <= month and p.last_day and p.last_day >= expected_end for p in periods)
        value = available_sum([f['amount'] for f in facts])
        if value is None and covered and all((s,month) in self.currency_periods for s in sources):
            value = Decimal(0)
        estimated = month == self.today.replace(day=1) or any(f['estimated'] for f in facts) or any(p and p.estimated for p in periods)
        state = 'No data' if value is None else 'Partial' if not covered else 'Estimated' if estimated else 'Complete'
        return {'value':value, 'state':state, 'estimated':estimated,
                'last_import':min((p.last_success for p in periods if p and p.last_success),default=None)}

    def load_services(self, month, keys):
        keys = set(keys)
        if not keys:
            return
        raw = self.costs.filter(day__gte=month-relativedelta(months=1),day__lt=month+relativedelta(months=1),
                               account_id__in={k[1] for k in keys},customer_id__in={k[0] for k in keys})
        for item in raw.annotate(period=TruncMonth('day')).values('customer_id','account_id','period','service').annotate(amount=Sum('unblended')):
            key = (item['customer_id'],item['account_id'])
            self.service_amounts.setdefault(key,{})[(item['service'],item['period'])] = item['amount']
        self.services_loaded.update(keys)

    def detail(self, key, month, threshold, extra_services=()):
        prior = month - relativedelta(months=1)
        current, previous = self.cell(key,month), self.cell(key,prior)
        if key not in self.services_loaded:
            self.load_services(month,[key])
        amounts = self.service_amounts.get(key,{})
        services = {s for s,m in amounts} | set(extra_services)
        rows = []
        for service in sorted(services):
            cur = amounts.get((service,month),Decimal(0) if current['state'] in ('Complete','Estimated') else None)
            prev = amounts.get((service,prior),Decimal(0) if previous['state'] in ('Complete','Estimated') else None)
            rows.append({'service':service,'current':cur,'prior':prev,**variance(cur,prev,threshold)})
        return {'current':current,'prior':previous,'services':rows,**variance(current['value'],previous['value'],threshold)}


def summary(month, currency, threshold, customer=None):
    fy = date(month.year if month.month >= 4 else month.year - 1, 4, 1)
    months = [fy + relativedelta(months=i) for i in range(12)]
    prior = month - relativedelta(months=1)
    data = SpendData(min(fy,prior), fy + relativedelta(years=1), currency, customer)
    latest_id = AllianceRecord.objects.filter(customer_id=OuterRef('customer_id'),account_id=OuterRef('account_id'),
        month__lte=month,currency=currency).order_by('-month').values('pk')[:1]
    records = AllianceRecord.objects.filter(pk=Subquery(latest_id),month__lte=month,currency=currency).select_related('account')
    if customer:
        records = records.filter(customer=customer)
    current, latest = {}, {}
    for record in records:
        key = (record.customer_id,record.account.account_id)
        latest.setdefault(key,record)
        if record.month == month:
            current[key] = record
            data.keys.add(key)
            data.accounts.setdefault(key[1],record.account)
    # Historical handoffs remain visible even if the account's ownership has ended.
    data.customers.update(Customer.objects.in_bulk({k[0] for k in data.keys} - data.customers.keys()))
    data.load_services(month,[key for key,record in current.items() if record.snapshot])
    rows = []
    for key in sorted(data.keys,key=lambda k:(data.customers[k[0]].name.lower(), k[1])):
        account = data.accounts.get(key[1])
        record, metadata = current.get(key), latest.get(key)
        cells = [{**data.cell(key,m),'selected':m==month} for m in months]
        values = [c['value'] for c in cells]
        count = sum(v is not None for v in values)
        total = available_sum(values)
        selected, previous = data.cell(key,month), data.cell(key,prior)
        row = {'customer':data.customers[key[0]], 'account_id':key[1], 'account':account,
               'name':metadata.account_name if metadata and metadata.account_name else (account.name if account and account.name else data.customers[key[0]].name),
               'legal_entity':metadata.legal_entity if metadata else '', 'ace':metadata.ace_opportunity_id if metadata else '',
               'cells':cells, 'fy_total':total,'average':total/count if count else None,'months_loaded':count,
               'current':selected,'prior':previous,'record':record, **variance(selected['value'],previous['value'],threshold)}
        captured = record.snapshot if record else {}
        row['recorded_spend'] = Decimal(captured['current']) if captured.get('current') is not None else None
        row['recorded_percent'] = Decimal(captured['percent']) if captured.get('percent') is not None else None
        row['revised'] = bool(captured) and captured.get('fingerprint') != snapshot(data.detail(key,month,threshold),threshold)['fingerprint']
        row['status'] = 'Cost revision needs review' if row['revised'] else record.status if record else 'Not started'
        row['missing_ids'] = not row['legal_entity'] or not row['ace']
        rows.append(row)
    return {'rows':rows,'months':months,'fy_start':fy,'fy_end':fy+relativedelta(years=1)-timedelta(days=1),'prior_month':prior}


def account_detail(customer, account, month, currency, threshold):
    prior = month - relativedelta(months=1)
    data = SpendData(prior,month+relativedelta(months=1),currency,customer)
    key = (customer.pk,account.account_id)
    existing = AllianceRecord.objects.filter(customer=customer,account=account,month=month,currency=currency).first()
    if key not in data.keys and not existing:
        raise ValueError('This account has no ownership or reporting history for this customer and period.')
    services = set()
    if existing:
        services.update(existing.service_notes.values_list('service',flat=True))
        services.update(x['service'] for x in existing.snapshot.get('services',[]))
    return {**data.detail(key,month,threshold,services),'record':existing}


def snapshot(detail, threshold):
    result = {'current':str(detail['current']['value']) if detail['current']['value'] is not None else None,
              'prior':str(detail['prior']['value']) if detail['prior']['value'] is not None else None,
              'state':detail['current']['state'], 'prior_state':detail['prior']['state'], 'threshold':str(threshold),
              'percent':str(detail['percent']) if detail['percent'] is not None else None,
              'services':[{'service':r['service'],'current':str(r['current']) if r['current'] is not None else None,'prior':str(r['prior']) if r['prior'] is not None else None} for r in detail['services']]}
    # Compare AWS figures, not the display threshold or decimal formatting. A
    # retained note for a retired zero-cost service is not a cost revision.
    def number(value):
        return str(Decimal(value).normalize()) if value is not None else None
    basis = {key:number(result[key]) for key in ('current','prior')}
    basis.update({key:result[key] for key in ('state','prior_state')})
    basis['services'] = [{key:number(item[key]) if key != 'service' else item[key] for key in item}
                         for item in result['services'] if any(item[k] is not None and Decimal(item[k]) != 0 for k in ('current','prior'))]
    result['fingerprint'] = sha256(json.dumps(basis,sort_keys=True).encode()).hexdigest()
    return result


def customer_rollups(rows, threshold=Decimal('15')):
    """Sum monetary cells only; recompute ratios and count non-additive statuses."""
    groups={}
    for row in rows:
        groups.setdefault(row['customer'].pk,{'customer':row['customer'],'rows':[]})['rows'].append(row)
    result=[]
    for group in groups.values():
        accounts=group['rows'];cells=[]
        for index in range(len(accounts[0]['cells'])):
            parts=[r['cells'][index] for r in accounts]
            value=available_sum([c['value'] for c in parts])
            cells.append({'value':value,'complete':all(c['state'] in ('Complete','Not owned') for c in parts),'estimated':any(c['estimated'] for c in parts)})
        current=available_sum([r['current']['value'] for r in accounts]);prior=available_sum([r['prior']['value'] for r in accounts])
        statuses={}
        for row in accounts:statuses[row['status']]=statuses.get(row['status'],0)+1
        total=available_sum([c['value'] for c in cells]);loaded=sum(c['value'] is not None for c in cells)
        result.append({**group,'cells':cells,'count':len(accounts),'current':current,'prior':prior,'fy_total':total,
                       'average':total/loaded if loaded else None,'statuses':statuses,**variance(current,prior,threshold)})
    return result
