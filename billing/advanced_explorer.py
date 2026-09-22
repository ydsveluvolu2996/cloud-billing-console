"""Cost Explorer reports backed by local imports or durable AWS request caches."""
from datetime import date, datetime, timedelta
from decimal import Decimal
from dateutil.relativedelta import relativedelta
from urllib.parse import urlencode
from django.utils import timezone
from . import parameters as contract
from . import scope as scoping
from .models import Customer, SavedReport
from .reporting import report
from .explorer import account_display_labels, explorer_report, CHART_COLORS, SERVICE_LABELS
from .query_cache import get_query


def is_local(p):
    return (p['metric'] in ('unblended','amortized') and p['measure']=='cost' and p['report_mode']=='standard'
        and p['granularity'] in ('daily','monthly') and p['group_by'] in ('service','account','customer')
        and date.fromisoformat(p['end'])<=timezone.now().date()
        and date.fromisoformat(p['start'])>=timezone.now().date().replace(day=1)-relativedelta(months=13) and p['untagged']=='0' and p['uncategorized']=='0'
        and not any(item['values'] or item['absent'] for item in contract.keyed_filters(p))
        and all(not p[k] for k in contract.FILTERS if k not in ('service','account'))
        and all(len(p[k])<=1 and contract.EMPTY_VALUE not in p[k] and (not p[k] or p[k+'_mode']=='include') for k in ('service','account')))


def periods_between(start,end,granularity):
    result=[];cursor=datetime.combine(start,datetime.min.time())
    if granularity=='monthly':cursor=cursor.replace(day=1)
    while cursor.date()<=end:
        result.append(cursor.strftime('%b %Y') if granularity=='monthly' else cursor.strftime('%Y-%m-%d %H:00') if granularity=='hourly' else cursor.date().isoformat())
        if granularity=='monthly':cursor=(cursor.replace(day=28)+timedelta(days=4)).replace(day=1)
        else:cursor+=timedelta(hours=1) if granularity=='hourly' else timedelta(days=1)
    return result


def unpack(queries,p,periods):
    values={}; estimated=False;units=set()
    metric=('NormalizedUsageAmount' if p['normalized']=='1' else 'UsageQuantity') if p['measure']=='usage' else contract.METRICS[p['metric']][1]
    for q in queries:
        if q.data is None:continue
        for period in q.data.get('ResultsByTime',[]):
            stamp=datetime.fromisoformat(period['TimePeriod']['Start'].replace('Z','+00:00'))
            label=stamp.strftime('%b %Y') if p['granularity']=='monthly' else stamp.strftime('%Y-%m-%d %H:00') if p['granularity']=='hourly' else stamp.date().isoformat()
            if label not in periods:raise ValueError('AWS returned a period outside the selected report.')
            estimated=estimated or period.get('Estimated',False)
            groups=period.get('Groups',[])
            if not groups and p['group_by'] in ('none','customer') and metric in period.get('Total',{}):groups=[{'Keys':[(q.customer.name if q.customer else q.source.customer.name) if p['group_by']=='customer' else 'Total'],'Metrics':period['Total']}]
            for group in groups:
                m=group['Metrics'][metric];units.add(m['Unit']);amount=Decimal(m['Amount'])
                if not amount.is_finite():raise ValueError('AWS returned a non-finite amount.')
                key=group['Keys'][0]
                if p['group_by']=='tag':
                    # AWS tags use Key$Value; leave category values opaque.
                    # Keep raw empty keys distinct from a real '(Unassigned)' value.
                    prefix=p['group_key']+'$'
                    if key.startswith(prefix):key=key[len(prefix):]
                bucket=values.setdefault(key,{})
                bucket[label]=bucket.get(label,Decimal(0))+amount
    if len(units)>1:raise ValueError('This report contains different units. Narrow its usage type or customer; different units cannot be added together.')
    if p['measure']=='cost' and units and units!={'USD'}:raise ValueError('AWS returned an unexpected currency; this report cannot be combined.')
    account_labels=account_display_labels(values,p['customer']) if p['group_by']=='account' else {}
    rows=[]
    for key,bucket in values.items():
        cells=[bucket.get(t) for t in periods]
        absent=p['group_by'] in ('tag','cost_category') and key==''
        label=SERVICE_LABELS.get(key,key) if p['group_by']=='service' else key or (f"No {contract.GROUPS[p['group_by']].lower()} key: {p['group_key']}" if absent else '(Not specified)')
        if p['group_by']=='account':label=account_labels.get(key,label)
        rows.append({'key':key,'label':label,'source_label':key,'is_absent':absent,'cells':cells,'total':sum((v for v in cells if v is not None),Decimal(0))})
    rows.sort(key=lambda r:(-r['total'],r['label']))
    return rows,estimated,next(iter(units),'USD' if p['measure']=='cost' else 'units')


def chart(rows,periods,p,unit):
    top=sorted(rows,key=lambda r:-sum(abs(v or 0) for v in r['cells']))[:9]
    remaining=[r for r in rows if r not in top]
    series=[{'label':r['label'],'values':[float(v) if v is not None else None for v in r['cells']],'color':CHART_COLORS[i]} for i,r in enumerate(top)]
    def totals(items):
        return [sum((r['cells'][i] or Decimal(0)) for r in items) if any(r['cells'][i] is not None for r in items) else None for i in range(len(periods))]
    if remaining:series.append({'label':'Others','values':[float(v) if v is not None else None for v in totals(remaining)],'color':CHART_COLORS[-1]})
    pt=totals(rows)
    return {'periods':periods,'series':series,'totals':[float(v) if v is not None else None for v in pt],'currency':unit,'measure':p['measure'],'style':p['chart_style']},pt


def unit_label(q):
    name=(q.customer or q.source.customer).name
    return name if q.customer or not q.source.shared else f'{name} (payer {q.source.account_id})'


def build_report(params):
    """Build either one measure or a bounded pair with independent units and totals."""
    p = contract.normalize(params)
    if p['measure'] != 'cost_usage':
        return _build_report(p)
    costs = _build_report(p | {'measure': 'cost', 'normalized': '0'}, display_params=p)
    usage = _build_report(p | {'measure': 'usage'}, display_params=p)
    costs['usage_report'] = usage
    costs['dual_measure'] = True
    costs['query_ids'] = list(dict.fromkeys(costs.get('query_ids', [])+usage.get('query_ids', [])))
    costs['report_pending'] = costs.get('report_pending', False) or usage.get('report_pending', False)
    costs['report_incomplete'] = costs.get('report_incomplete', False) or usage.get('report_incomplete', False)
    costs['warnings'] = list(dict.fromkeys(costs.get('warnings', [])+usage.get('warnings', [])))
    costs['capability_notes'] = list(dict.fromkeys(costs.get('capability_notes', [])+usage.get('capability_notes', [])))
    snapshots = [costs.get('report_snapshot', ''), usage.get('report_snapshot', '')]
    costs['report_snapshot'] = 'Costs: '+snapshots[0]+'; Usage: '+snapshots[1]
    costs['last_sync'] = min(costs['last_sync'], usage['last_sync']) if costs.get('last_sync') and usage.get('last_sync') else None
    return costs


def _build_report(params, display_params=None):
    p=contract.normalize(params);today=timezone.now().date();start=date.fromisoformat(p['start']);end=date.fromisoformat(p['end'])
    local_params={'start':p['start'],'end':str(min(end,today)),'customer':p['customer'],'source':p['source'],'currency':p['currency'],
                  'granularity':p['granularity'] if p['granularity']!='hourly' else 'daily',
                  'metric':p['metric'] if p['metric'] in ('unblended','amortized') else 'unblended'}
    if is_local(p):
        local_params.update({k:next(iter(p[k]),'') for k in ('service','account')});local_params.update(group_by=p['group_by'],chart_style=p['chart_style'])
    else:
        # This local context supplies connection status; actual rows below come from
        # AWS. Keep the legacy portfolio helper inside its two-year validation limit.
        local_params.update(start=str(max(start,min(end,today)-timedelta(days=731))),
                            group_by='service',chart_style=p['chart_style'])
    context=explorer_report(report(local_params),local_params)
    queries=[];actual=[];previous=[];forecast=[];warnings=[];blocked=[]
    def queue(target, source, operation, request, customer, accounts):
        try:
            target.append(get_query(source,operation,request,customer=customer,account_filter=accounts))
        except ValueError as exc:
            message=f'{(customer or source.customer).name}: {exc}'
            blocked.append(message)
            if message not in warnings:warnings.append(message)
    if not is_local(p):
        scope=scoping.resolve(p)
        selected=Customer.objects.filter(active=True) if not scope.customer else Customer.objects.filter(pk=scope.customer.pk)
        # One AWS request per (connection, customer scope). Shared payers carry a LINKED_ACCOUNT
        # restriction derived from account assignments; portfolio reports query each connection once.
        range_start=min(start,date.fromisoformat(p['compare_start'])) if p['report_mode']=='compare' else start
        range_end=max(end,date.fromisoformat(p['compare_end'])) if p['report_mode']=='compare' else end
        if scope.customer or p['group_by']=='customer':
            units=[u for c in selected for u in scoping.report_units(c,start=range_start,end=range_end+timedelta(days=1))]
        else:
            units=scoping.report_units(None,start=range_start,end=range_end+timedelta(days=1))
        if scope.source:units=[u for u in units if u[0].pk==scope.source.pk]
        # Account selections are report filters, including NOT and multiselect. Ownership
        # restrictions come only from report_units/ownership_windows and apply to both periods.
        # Turning an excluded account into the query scope produces A AND NOT A (zero costs).
        covered={u[0].customer_id for u in units}|{u[1].pk for u in units if u[1]}
        from .models import BillingSource
        expected=set(BillingSource.objects.exclude(role_arn='').values_list('customer_id',flat=True))
        unavailable=sum(1 for c in selected if c.pk not in covered and (c.pk in expected or scope.customer)) if not scope.source else int(not units)
        setup=sum(1 for c in selected if c.pk not in covered and c.pk not in expected) if not scope.source else 0
        if unavailable:warnings.append(f'{unavailable} customer(s) have unavailable billing connections. This report cannot yet cover their costs.')
        if setup:warnings.append(f'{setup} customer(s) awaiting onboarding are excluded. This report covers connected customers only.')
        actual_end=min(end,today-timedelta(days=1) if end>today else today)
        period_labels=periods_between(start,actual_end,p['granularity'])
        prior_periods=periods_between(date.fromisoformat(p['compare_start']),date.fromisoformat(p['compare_end']),p['granularity']) if p['report_mode']=='compare' else []
        for source,customer,accounts in units:
            if start<=actual_end:
                for left,right,owned in scoping.ownership_windows(source,customer,start,actual_end+timedelta(days=1)):
                    selected_accounts = sorted(set(owned)&set(accounts)) if owned is not None and accounts is not None else owned if owned is not None else accounts
                    op,req=contract.aws_request(p,start=str(left),end=str(right-timedelta(days=1)))
                    queue(actual,source,op,req,customer,selected_accounts)
            if p['report_mode']=='compare':
                for left,right,owned in scoping.ownership_windows(source,customer,date.fromisoformat(p['compare_start']),date.fromisoformat(p['compare_end'])+timedelta(days=1)):
                    op,req=contract.aws_request(p,start=str(left),end=str(right-timedelta(days=1)))
                    selected_accounts = sorted(set(owned)&set(accounts)) if owned is not None and accounts is not None else owned if owned is not None else accounts
                    queue(previous,source,op,req,customer,selected_accounts)
            if end>today and p['forecast']=='1':
                for left,right,owned in scoping.ownership_windows(source,customer,today,end+timedelta(days=1)):
                    req={'TimePeriod':{'Start':str(left),'End':str(right)},'Granularity':p['granularity'].upper(),'Metric':contract.FORECAST_METRICS[p['metric']],'PredictionIntervalLevel':80}
                    exp=contract.expression(p)
                    if exp:req['Filter']=exp
                    queue(forecast,source,'get_cost_forecast',req,customer,owned)
        queries=actual+previous+forecast
        rows,estimated,unit=unpack(actual,p,period_labels)
        payload,pt=chart(rows,period_labels,p,unit)
        total=sum((r['total'] for r in rows),Decimal(0))
        context.update(pivot_rows=rows,periods=period_labels,period_totals=pt,total=total,group_count=len(rows),
                       period_average=total/len(period_labels) if rows and period_labels else None,
                       has_data=bool(rows),estimated=estimated,chart_payload=payload,chart_series=payload['series'],currency=unit,
                       missing_days=0,filter_service='',filter_account='',last_sync=min((q.last_success for q in actual if q.last_success),default=None))
        if p['report_mode']=='compare':
            prior,_,prior_unit=unpack(previous,p,prior_periods)
            if rows and prior and prior_unit!=unit:raise ValueError('Comparison periods have different units.')
            before={r['key']:r for r in prior};after={r['key']:r for r in rows};comparisons=[]
            ready=not blocked and not unavailable and bool(units) and all(q.data is not None for q in actual+previous)
            for key in sorted(before.keys()|after.keys()):
                left=before.get(key,{}).get('total',Decimal(0)) if ready else None
                right=after.get(key,{}).get('total',Decimal(0)) if ready else None
                delta=right-left if ready else None
                comparisons.append({'key':key,'label':(after.get(key) or before[key])['label'],'before':left,'after':right,'delta':delta,'percent':delta/abs(left)*100 if left else None})
            context.update(comparison_rows=comparisons,comparison_ready=ready,comparison_total=sum((r['total'] for r in prior),Decimal(0)) if ready else None)
            if ready:
                before_total=context['comparison_total'];delta=total-before_total
                ordered=sorted(comparisons,key=lambda r:-abs(r['delta']))
                displayed=ordered[:9]
                if len(ordered)>9:
                    rest=ordered[9:]
                    displayed.append({'label':'Others','before':sum(r['before'] for r in rest),'after':sum(r['after'] for r in rest),'delta':sum(r['delta'] for r in rest)})
                comparison_chart={'periods':[r['label'] for r in displayed],
                    'series':[{'label':label,'values':[float(r[key]) for r in displayed],'color':color} for label,key,color in [('Previous','before',CHART_COLORS[0]),('Selected','after',CHART_COLORS[2])]],
                    'totals':[float(r['delta']) for r in displayed],'currency':unit,'measure':p['measure'],'style':'bar','comparison':True}
                context.update(comparison_delta=delta,comparison_percent=delta/abs(before_total)*100 if before_total else None,comparison_chart=comparison_chart)
        frows=[]
        for q in forecast:
            if q.data:
                for period in q.data.get('ForecastResultsByTime',[]):
                    frows.append({'series_id':str(q.pk),'customer':unit_label(q),'start':period['TimePeriod']['Start'],'end':period['TimePeriod']['End'],
                                  'mean':Decimal(period['MeanValue']),'lower':Decimal(period.get('PredictionIntervalLowerBound','0')),'upper':Decimal(period.get('PredictionIntervalUpperBound','0'))})
        context.update(forecast_rows=frows,forecast_requested=end>today and p['forecast']=='1',actual_end=actual_end,aws_report=True)
        payload.update(forecast_rows=[{**row, **{key:float(row[key]) for key in ('mean','lower','upper')}} for row in frows],
                       forecast_requested=context['forecast_requested'], forecast_interval=80)
        for q in queries:
            if q.error:warnings.append(unit_label(q)+': '+q.error+(' Previous cached figures are displayed.' if q.data is not None else ''))
        context.update(report_pending=any(q.requested for q in queries),query_ids=[q.pk for q in queries],
                       report_incomplete=bool(blocked) or not units or unavailable>0 or any(q.data is None for q in queries),
                       report_snapshot='; '.join(f'{unit_label(q)}: {q.last_success.isoformat() if q.last_success else "pending"}' for q in actual))
        context['known_empty']=not context['report_incomplete'] and not rows
        if context['known_empty']:
            context.update(period_average=Decimal(0) if period_labels else None,period_totals=[Decimal(0) for _ in period_labels])
        if p['report_mode']=='compare':
            from .comparison_drivers import build as driver_report
            drivers=driver_report(p,units)
            context.update(driver_rows=drivers['rows'],driver_notes=drivers['notes'])
            context['query_ids'] += [q.pk for q in drivers['queries']]
            context['report_pending'] = context['report_pending'] or any(q.requested for q in drivers['queries'])
    display_params=display_params or p
    q=contract.querydict(display_params)
    def url(**changes):
        new=display_params|changes
        return '/?'+contract.querydict(new).urlencode()
    for row in context['pivot_rows']+context.get('comparison_rows',[]):
        group=p['group_by']
        # Discard legacy portfolio links before constructing the complete Explorer URL.
        row.pop('url',None)
        if group in contract.FILTERS and not (group in ('tag','cost_category') and row['key']==''):
            value=contract.EMPTY_VALUE if row['key']=='' or (group=='region' and row['key']=='NoRegion') else row['key']
            changes={group:[value],group+'_mode':'include'}
            if group in ('tag','cost_category'):
                changes=contract.keyed_drilldown(p,group,p['group_key'],value)
            row['url']=url(**changes)
        elif group in ('tag','cost_category') and row['key']=='':
            row['url']=url(**contract.keyed_drilldown(p,group,p['group_key'],absent=True))
        elif group=='customer':
            customer=Customer.objects.filter(pk=row['key']).first() if is_local(p) else Customer.objects.filter(name=row['key']).first()
            if customer:row['url']=url(customer=str(customer.pk))
        if row.get('url') and 'before' in row:
            from django.http import QueryDict
            drill=QueryDict(row['url'].split('?',1)[1],mutable=True)
            drill['group_by']='usage_type' if group=='service' else 'service';drill['group_key']=''
            row['url']='/?'+drill.urlencode()
    filters=[]
    for key,(label,_) in contract.FILTERS.items():
        filters.append({'key':key,'label':label,'values':p[key],'mode':p[key+'_mode'],'key_value':p.get(key+'_key',''),
                        'expanded':bool(p[key]) or p.get({'tag':'untagged','cost_category':'uncategorized'}.get(key,''))=='1','is_keyed':key in ('tag','cost_category'),'is_more':list(contract.FILTERS).index(key)>8})
    from django.conf import settings
    capability_notes=[]
    if start < today.replace(day=1)-relativedelta(months=13):
        capability_notes.append('History beyond the default 13 prior months requires AWS multi-year data already enabled. Up to 38 months is available at Monthly granularity; this console does not change AWS billing preferences.')
    if settings.REQUIRE_CONNECTION_APPROVAL:
        sources={s.pk:s for s,_,_ in scoping.report_units(scoping.resolve(p).customer)}
        if p['source']:sources={k:s for k,s in sources.items() if str(k)==p['source']}
        for cap,label,requirement in [('forecasts','Forecasts','ce:GetCostForecast'),('tags','Tags and untagged reports','approved tag keys, ce:GetTags and ce:ListCostAllocationTags'),('cost_categories','Cost categories','approved category keys and ce:GetCostCategories'),('resources','Resource reports','ce:GetCostAndUsageWithResources and AWS resource-data opt-in')]:
            missing=[s.customer.name for s in sources.values() if not s.capabilities.get(cap)]
            if missing:capability_notes.append(f'{label}: requires {requirement}. Not enabled for: {", ".join(sorted(set(missing)))}.')
    context.update(params=display_params,parameters_json=display_params,capability_notes=capability_notes,parameter_fields={k:v for k,v in display_params.items() if not isinstance(v,list)},
                   parameter_pairs=list(q.lists()),filters=filters,additional_keyed_filters=contract.keyed_filters(p),applied_count=sum(bool(p[k]) for k in contract.FILTERS)+int(p['untagged']=='1')+int(p['uncategorized']=='1')+sum(bool(item['values']) or item['absent'] for item in contract.keyed_filters(p)),
                   start=start,end=end,today=today,granularity=p['granularity'],group_by=p['group_by'],group_label=contract.GROUPS[p['group_by']],
                   metric=p['metric'],metric_label=contract.METRICS[p['metric']][0] if p['measure']=='cost' else 'Normalized usage' if p['normalized']=='1' else 'Usage quantity',
                   measure=p['measure'],measure_label='Cost' if p['measure']=='cost' else 'Usage',chart_style=p['chart_style'],
                   export_query=q.urlencode(),style_options=[{'label':s.title(),'value':s,'url':url(chart_style=s)} for s in ('bar','line','stacked')],
                   date_range_options=list(contract.DATE_RANGES.items()),future_range_options=list(contract.FUTURE_RANGES.items()),
                   group_options=list(contract.GROUPS.items()),metric_options=[(k,v[0]) for k,v in contract.METRICS.items()],
                   warnings=warnings,customers=Customer.objects.filter(active=True),saved_reports=SavedReport.objects.filter(archived_at__isnull=True),
                   filter_customer=p['customer'],active_page='explorer',clear_url=url(**{k:[] for k in contract.FILTERS},untagged='0',uncategorized='0',keyed_filters='[]'),
                   report_presets=[{'label':label,'url':url(date_range=v)} for label,v in [('This month','this_month'),('Last 3 months','last_3_months'),('Last 6 months','last_6_months')]])
    return context
