"""Validated report contract shared by HTML, CSV, saved reports and the worker."""
import json
from datetime import date, timedelta
from urllib.parse import parse_qs, urlencode, urlsplit
from dateutil.relativedelta import relativedelta
from django.http import QueryDict
from django.utils import timezone

# The console's complete filter inventory, mapped to documented CE dimensions.
DIMENSIONS = {
    'service': ('Service', 'SERVICE'), 'account': ('Linked account', 'LINKED_ACCOUNT'),
    'region': ('Region', 'REGION'), 'instance_type': ('Instance type', 'INSTANCE_TYPE'),
    'usage_type': ('Usage type', 'USAGE_TYPE'), 'usage_type_group': ('Usage type group', 'USAGE_TYPE_GROUP'),
    'resource': ('Resource', 'RESOURCE_ID'), 'charge_type': ('Charge type', 'RECORD_TYPE'),
    'az': ('Availability zone', 'AZ'), 'platform': ('Platform', 'PLATFORM'),
    'purchase_option': ('Purchase option', 'PURCHASE_TYPE'), 'tenancy': ('Tenancy', 'TENANCY'),
    'database_engine': ('Database engine', 'DATABASE_ENGINE'), 'legal_entity': ('Legal entity', 'LEGAL_ENTITY_NAME'),
    'billing_entity': ('Billing entity', 'BILLING_ENTITY'), 'operation': ('API operation', 'OPERATION'),
    'payer': ('Payer account', 'PAYER_ACCOUNT'),
}
FILTERS = dict(list(DIMENSIONS.items())[:7]) | {'cost_category': ('Cost category', 'COST_CATEGORY'), 'tag': ('Tag', 'TAG')} | dict(list(DIMENSIONS.items())[7:])
GROUPS = {'none': 'None', 'customer': 'Customer'} | {k:v[0] for k,v in FILTERS.items() if k != 'usage_type_group'}
METRICS = {'unblended': ('Unblended costs', 'UnblendedCost'), 'amortized': ('Amortized costs', 'AmortizedCost'),
           'blended': ('Blended costs', 'BlendedCost'), 'net_unblended': ('Net unblended costs', 'NetUnblendedCost'),
           'net_amortized': ('Net amortized costs', 'NetAmortizedCost')}
FORECAST_METRICS = dict(zip(METRICS, ['UNBLENDED_COST','AMORTIZED_COST','BLENDED_COST','NET_UNBLENDED_COST','NET_AMORTIZED_COST']))
EC2 = 'Amazon Elastic Compute Cloud - Compute'
EMPTY_VALUE = '__billing_empty_value__'
DATE_RANGES = {
    'last_1_day': 'Last 1 day', 'last_7_days': 'Last 7 days', 'last_14_days': 'Last 14 days',
    'this_month': 'Month to date', 'last_month': 'Last 1 month',
    'last_3_months': 'Last 3 months', 'last_6_months': 'Last 6 months',
    'last_12_months': 'Last 1 year', 'last_36_months': 'Last 3 years',
    'year_to_date': 'Year to date', 'current_month': 'Current month', 'custom': 'Custom dates',
}
FUTURE_RANGES = {'none': 'No forecast range', 'next_1_month': 'Next 1 month',
                 'next_3_months': 'Next 3 months', 'next_12_months': 'Next 12 months',
                 'next_18_months': 'Next 18 months'}
HISTORICAL_CONSOLE_RANGES = {
    'LAST_1_DAY': 'last_1_day', 'LAST_7_DAYS': 'last_7_days', 'LAST_14_DAYS': 'last_14_days',
    'MONTH_TO_DATE': 'this_month', 'THIS_MONTH': 'this_month',
    'LAST_1_MONTH': 'last_month', 'LAST_MONTH': 'last_month',
    'LAST_3_MONTHS': 'last_3_months', 'LAST_6_MONTHS': 'last_6_months',
    'LAST_1_YEAR': 'last_12_months', 'LAST_12_MONTHS': 'last_12_months',
    'LAST_3_YEARS': 'last_36_months', 'LAST_36_MONTHS': 'last_36_months',
    'YEAR_TO_DATE': 'year_to_date', 'CURRENT_MONTH': 'current_month',
}
FUTURE_CONSOLE_RANGES = {'NEXT_MONTH': 'next_1_month', 'NEXT_1_MONTH': 'next_1_month', 'NEXT_3_MONTHS': 'next_3_months',
                         'NEXT_12_MONTHS': 'next_12_months', 'NEXT_18_MONTHS': 'next_18_months'}


def date_parameters(params, today=None):
    """Resolve the historical and future selections without validating unrelated fields."""
    today = today or timezone.now().date()
    month = today.replace(day=1)
    selected = params.get('date_range') or ('custom' if params.get('start') and params.get('end') else 'last_6_months')
    future = params.get('future_range') or 'none'
    if selected not in DATE_RANGES or future not in FUTURE_RANGES:
        raise ValueError('Choose valid historical and forecast date ranges.')
    if selected in ('last_3_months', 'last_6_months', 'last_12_months', 'last_36_months'):
        count = int(selected.split('_')[1])
        start, end = month-relativedelta(months=count), month-timedelta(days=1)
    elif selected == 'custom':
        try:
            start = date.fromisoformat(params.get('start', ''))
            end = date.fromisoformat((params.get('historical_end') if future != 'none' else '') or params.get('end', ''))
        except (ValueError, TypeError):
            raise ValueError('Choose valid start and end dates.') from None
    else:
        ranges = {
            'last_1_day': (today-timedelta(days=1), today-timedelta(days=1)),
            'last_7_days': (today-timedelta(days=6), today),
            'last_14_days': (today-timedelta(days=13), today),
            'this_month': (month, today),
            'last_month': (month-relativedelta(months=1), month-timedelta(days=1)),
            'year_to_date': (today.replace(month=1, day=1), today),
            'current_month': (month, month+relativedelta(months=1)-timedelta(days=1)),
        }
        start, end = ranges[selected]
    if start > end:
        raise ValueError('The historical start date must be on or before its end date.')
    historical_end = end
    if future != 'none':
        count = int(future.split('_')[1])
        end = max(end, month+relativedelta(months=count+1)-timedelta(days=1))
    return {'date_range': selected, 'future_range': future, 'start': str(start),
            'end': str(end), 'historical_end': str(historical_end)}


def truth(value):
    return str(value).lower() in ('true', '1', 'on')


def values(params, key):
    raw = params.getlist(key) if hasattr(params, 'getlist') else params.get(key, [])
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or any(not isinstance(v, str) or len(v)>1024 for v in raw) or len(raw)>100:
        raise ValueError('Each filter supports up to 100 values of at most 1,024 characters.')
    return sorted(set(v for v in raw if v != ''))


def keyed_filters(params):
    """Validate additional keyed filters; the legacy primary filters remain supported."""
    raw = params.get('keyed_filters') or []
    if isinstance(raw, str):
        if len(raw) > 200000:
            raise ValueError('Additional tag/category filters are too large.')
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            raise ValueError('Additional tag/category filters must be valid JSON.') from None
    if not isinstance(raw, list) or len(raw) > 20:
        raise ValueError('Choose at most 20 additional tag/category keys.')
    result = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict) or set(item)-{'type', 'key', 'values', 'mode', 'absent'}:
            raise ValueError('Choose a valid additional tag/category filter.')
        kind, key = item.get('type'), item.get('key')
        mode = item.get('mode', 'include')
        if kind not in ('tag', 'cost_category') or not isinstance(key, str) or not key or len(key) > 120:
            raise ValueError('Each additional filter requires a tag or category key of up to 120 characters.')
        if mode not in ('include', 'exclude') or not isinstance(item.get('absent', False), bool):
            raise ValueError('Additional filters require Include/Exclude and a valid absence choice.')
        if (kind, key) in seen:
            raise ValueError('Choose each tag/category key only once.')
        seen.add((kind, key))
        selected = item.get('values', [])
        if not isinstance(selected, list) or len(selected) > 100 or any(not isinstance(v, str) or len(v) > 1024 for v in selected):
            raise ValueError('Each additional filter supports up to 100 values of at most 1,024 characters.')
        selected = sorted(set(EMPTY_VALUE if value == '' else value for value in selected))
        absent = item.get('absent', False)
        if selected and absent:
            raise ValueError('Clear selected values before choosing an absent tag/category key.')
        result.append({'type': kind, 'key': key, 'values': selected, 'mode': mode, 'absent': absent})
    if len(json.dumps(result, ensure_ascii=False)) > 200000:
        raise ValueError('Additional tag/category filters are too large.')
    return sorted(result, key=lambda item: (item['type'], item['key']))


def normalize(params, today=None):
    today = today or timezone.now().date()
    p = {k:params.get(k, '') for k in ('customer','source','currency','start','end','granularity','group_by','group_key','chart_style','metric','report_mode','compare_start','compare_end','tag_key','cost_category_key','report_name','date_range','future_range','historical_end','keyed_filters','measure','forecast','untagged','uncategorized','normalized')}
    p.update(currency=p['currency'] or 'USD', granularity=p['granularity'] or 'monthly', group_by=p['group_by'] or 'service', chart_style=p['chart_style'] or 'stacked', metric=p['metric'] or 'unblended', report_mode=p['report_mode'] or 'standard', measure=p['measure'] or 'cost', report_name=p['report_name'] or 'Cost report')
    p['compare_range']=params.get('compare_range') or ('custom' if p['compare_start'] or p['compare_end'] else 'previous_period')
    if p['compare_range'] not in ('custom','previous_period','month_over_month'):
        raise ValueError('Choose a valid comparison range.')
    if p['report_mode']=='compare' and p['compare_range']=='month_over_month':
        p.update(date_range='last_month',granularity='monthly',compare_start=str(today.replace(day=1)-relativedelta(months=2)),compare_end=str(today.replace(day=1)-relativedelta(months=1)-timedelta(days=1)))
    p.update(date_parameters(p, today))
    start, end = date.fromisoformat(p['start']), date.fromisoformat(p['end'])
    if start > end or start > today or end > today.replace(day=1)+relativedelta(months=19)-timedelta(days=1):
        raise ValueError('Choose a range starting today or earlier, with forecasts up to 18 months ahead.')
    if start < today.replace(day=1)-relativedelta(months=38):
        raise ValueError('AWS monthly history covers up to 38 months when multi-year data is already enabled.')
    if (min(end, today)-start).days > 731 and p['granularity'] != 'monthly':
        raise ValueError('Reports over two years require Monthly granularity and AWS multi-year data already enabled.')
    if p['granularity'] not in ('daily','monthly','hourly') or p['group_by'] not in GROUPS or p['chart_style'] not in ('stacked','line','bar') or p['metric'] not in METRICS or p['report_mode'] not in ('standard','compare') or p['measure'] not in ('cost','usage','cost_usage'):
        raise ValueError('Choose valid report, chart, grouping and metric options.')
    if p['currency'] != 'USD':
        # AWS CE API returns USD; local imported reports may still support other currencies.
        raise ValueError('AWS Cost Explorer reports use USD. Other imported currencies are available in Customer overview.')
    for key in ('group_key','tag_key','cost_category_key','report_name'):
        if len(p[key])>120: raise ValueError('Report names and tag/category keys must be 120 characters or fewer.')
    if 'forecast' not in params:p['forecast']='1'
    for k in ('forecast','untagged','uncategorized','normalized'):
        p[k]='1' if truth(p[k]) else '0'
    for key in FILTERS:
        p[key]=values(params,key)
        mode=params.get(key+'_mode','include') or 'include'
        if mode not in ('include','exclude'): raise ValueError('Filter matching must be include or exclude.')
        p[key+'_mode']=mode
    additional = keyed_filters(p)
    for item in additional:
        kind = item['type']
        if item['key'] == p[kind+'_key'] and (p[kind] or p['untagged' if kind=='tag' else 'uncategorized']=='1'):
            raise ValueError('Choose each tag/category key only once, including the primary filter.')
    p['keyed_filters'] = json.dumps(additional, separators=(',', ':'), ensure_ascii=False)
    if p['group_by'] in ('tag','cost_category') and not p['group_key']:
        raise ValueError('Choose a tag or cost category key to group by.')
    for key,flag in [('tag','untagged'),('cost_category','uncategorized')]:
        if p[key] and not p[key+'_key']:
            raise ValueError(f'Choose the {FILTERS[key][0].lower()} key for this filter.')
        if p[key] and p[flag]=='1': raise ValueError('Clear the selected values before choosing only untagged or uncategorized resources.')
    if p['measure'] in ('usage','cost_usage') and not any(len(p[k])==1 and p[k+'_mode']=='include' for k in ('usage_type','usage_type_group')):
        raise ValueError('Select one usage type or usage type group before viewing usage so different units are not added together.')
    if p['normalized']=='1' and p['measure'] not in ('usage','cost_usage'):
        raise ValueError('Choose Usage quantity to view normalized units.')
    resource = p['resource'] or p['group_by']=='resource'
    if resource and (p['service'] != [EC2] or p['service_mode']!='include'):
        raise ValueError('Resource reports require Service = Amazon Elastic Compute Cloud - Compute (EC2-Instances).')
    if (resource or p['granularity']=='hourly') and (start<today-timedelta(days=13) or end>today):
        raise ValueError('Resource and hourly reports require dates within the last 14 days and enabled AWS granular data.')
    if p['report_mode']=='compare':
        if p['compare_range']=='previous_period':p.update(compare_start='',compare_end='')
        if bool(p['compare_start']) != bool(p['compare_end']):
            raise ValueError('Enter both comparison dates, or leave both blank for the preceding period.')
        if not p['compare_start'] or not p['compare_end']:
            if start.day==1 and (end+timedelta(days=1)).day==1:
                months=(end.year-start.year)*12+end.month-start.month+1
                p['compare_start']=str(start-relativedelta(months=months));p['compare_end']=str(start-timedelta(days=1))
            else:
                p['compare_start']=str(start-timedelta(days=(end-start).days+1));p['compare_end']=str(start-timedelta(days=1))
        try: cs,ce=date.fromisoformat(p['compare_start']),date.fromisoformat(p['compare_end'])
        except (ValueError,TypeError): raise ValueError('Choose valid comparison dates.')
        if cs>ce or ce>today: raise ValueError('Comparison must end today or earlier.')
        if cs<today.replace(day=1)-relativedelta(months=38):
            raise ValueError('Comparison history covers up to 38 months when AWS multi-year data is already enabled.')
        if (ce-cs).days>731 and p['granularity']!='monthly':
            raise ValueError('Comparison ranges over two years require Monthly granularity and AWS multi-year data already enabled.')
        if (resource or p['granularity']=='hourly') and cs<today-timedelta(days=13): raise ValueError('Comparison must also be within the last 14 days for resource/hourly reports.')
    if end>today:
        if p['measure']=='cost_usage':
            raise ValueError('The combined cost and usage view currently supports historical dates. Choose Month to date or an earlier end date; future usage forecasts are not available here.')
        if p['forecast']!='1' or p['measure']!='cost' or p['report_mode']=='compare': raise ValueError('Future dates require forecasted costs in Standard mode.')
        if p['granularity']=='daily' and end>today+relativedelta(months=3): raise ValueError('Daily forecasts cover up to three months.')
    return p


def keyed_drilldown(p, kind, key, value=None, absent=False):
    """Select one grouped key without dropping a different primary keyed filter."""
    entries = [item for item in keyed_filters(p) if (item['type'], item['key']) != (kind, key)]
    flag = 'untagged' if kind == 'tag' else 'uncategorized'
    old_key = p[kind+'_key']
    if old_key and old_key != key and (p[kind] or p[flag]=='1'):
        entries.append({'type': kind, 'key': old_key, 'values': p[kind],
                        'mode': p[kind+'_mode'], 'absent': p[flag]=='1'})
    return {kind: [] if absent else [value], kind+'_key': key, kind+'_mode': 'include',
            flag: '1' if absent else '0', 'keyed_filters': json.dumps(entries, separators=(',', ':'), ensure_ascii=False)}


def expression(p):
    parts=[]
    for key,(_,aws_key) in FILTERS.items():
        if key in ('tag','cost_category'):
            category='Tags' if key=='tag' else 'CostCategories'
            if p['untagged' if key=='tag' else 'uncategorized']=='1':
                absent={'MatchOptions':['ABSENT']}
                if p[key+'_key']:absent['Key']=p[key+'_key']
                absent_expr={category:absent}
                parts.append({'Not':absent_expr} if p[key+'_mode']=='exclude' else absent_expr);continue
            expr={category:{'Key':p[key+'_key'],'Values':['' if v==EMPTY_VALUE else v for v in p[key]]}}
        else:
            expr={'Dimensions':{'Key':aws_key,'Values':['' if v==EMPTY_VALUE else v for v in p[key]]}}
        if p[key]: parts.append({'Not':expr} if p[key+'_mode']=='exclude' else expr)
    for item in keyed_filters(p):
        if not item['values'] and not item['absent']:
            continue
        term = {'Key': item['key']}
        if item['absent']:
            term['MatchOptions'] = ['ABSENT']
        else:
            term['Values'] = ['' if v == EMPTY_VALUE else v for v in item['values']]
        expr = {'Tags' if item['type'] == 'tag' else 'CostCategories': term}
        parts.append({'Not': expr} if item['mode'] == 'exclude' else expr)
    return {'And':parts} if len(parts)>1 else parts[0] if parts else None


def aws_request(p, start=None, end=None):
    start=start or p['start']; end=end or p['end']
    metric = ('NormalizedUsageAmount' if p['normalized']=='1' else 'UsageQuantity') if p['measure']=='usage' else METRICS[p['metric']][1]
    req={'TimePeriod':{'Start':start,'End':str(date.fromisoformat(end)+timedelta(days=1))},'Granularity':p['granularity'].upper(),'Metrics':[metric]}
    if p['granularity']=='hourly':
        req['TimePeriod']={key:value+'T00:00:00Z' for key,value in req['TimePeriod'].items()}
    exp=expression(p)
    if exp:req['Filter']=exp
    g=p['group_by']
    if g not in ('none','customer'):
        req['GroupBy']=[{'Type':'TAG' if g=='tag' else 'COST_CATEGORY' if g=='cost_category' else 'DIMENSION','Key':p['group_key'] if g in ('tag','cost_category') else DIMENSIONS[g][1]}]
    operation='get_cost_and_usage_with_resources' if p['resource'] or g=='resource' else 'get_cost_and_usage'
    return operation,req


def querydict(p):
    q=QueryDict(mutable=True)
    for key,value in p.items():
        if isinstance(value,list):q.setlist(key,value)
        elif value!='':q[key]=str(value)
    return q


def import_console_url(url, today=None):
    """Import documented console link fields without fetching an arbitrary URL."""
    parsed=urlsplit(url)
    if parsed.scheme!='https' or not parsed.hostname or not (parsed.hostname=='console.aws.amazon.com' or parsed.hostname.endswith('.console.aws.amazon.com')) or 'cost-explorer?' not in parsed.fragment:
        raise ValueError('Paste an HTTPS AWS Cost Explorer report URL.')
    raw=parse_qs(parsed.fragment.split('?',1)[1],keep_blank_values=True)
    get=lambda key,default='':raw.get(key,[default])[0]
    compare=get('reportMode','STANDARD')=='COMPARE'
    p={'start':get('comparisonStartDate') if compare else get('startDate'),'end':get('comparisonEndDate') if compare else get('endDate'),'date_range':'custom',
       'compare_start':get('baselineStartDate') if compare else '', 'compare_end':get('baselineEndDate') if compare else '',
       'granularity':get('granularity','Monthly').lower(),'report_mode':get('reportMode','STANDARD').lower(),
       'report_name':get('reportName','Cost report'),'chart_style':{'STACK':'stacked','BAR':'bar','LINE':'line'}.get(get('chartStyle','STACK'),'invalid'),
       'metric':{'unBlendedCost':'unblended','amortizedCost':'amortized','blendedCost':'blended','netUnblendedCost':'net_unblended','netAmortizedCost':'net_amortized'}.get(get('costAggregate','unBlendedCost'),'invalid'),
       'forecast':'0' if truth(get('excludeForecasting','false')) else '1','untagged':get('showOnlyUntagged','false'),
       'uncategorized':get('showOnlyUncategorized','false'),'normalized':get('useNormalizedUnits','false')}
    if compare and get('compareRelativeRange')=='MONTH_OVER_MONTH':p['compare_range']='month_over_month'
    if not compare:
        historical = get('historicalRelativeRange')
        future = get('futureRelativeRange')
        if historical in HISTORICAL_CONSOLE_RANGES:
            p['date_range'] = HISTORICAL_CONSOLE_RANGES[historical]
        elif historical not in ('', 'CUSTOM', 'CUSTOM_RANGE', 'undefined', 'null'):
            raise ValueError('Unsupported historical relative range in the AWS report URL.')
        if future in FUTURE_CONSOLE_RANGES:
            p['future_range'] = FUTURE_CONSOLE_RANGES[future]
        elif future not in ('', 'NONE', 'CUSTOM', 'CUSTOM_RANGE', 'undefined', 'null'):
            raise ValueError('Unsupported forecast relative range in the AWS report URL.')
    canonical=lambda value:''.join(c for c in value.lower() if c.isalnum())
    group_alias={canonical(label):key for key,label in GROUPS.items()}
    aliases={canonical(label):key for key,(label,_) in FILTERS.items()}
    aliases.update({canonical(dim):key for key,(_,dim) in DIMENSIONS.items()})
    aliases.update({'linkedaccount':'account','availabilityzone':'az','purchasetype':'purchase_option','recordtype':'charge_type','tagkey':'tag','costcategorykey':'cost_category'})
    group_alias.update({k:v for k,v in aliases.items() if v in GROUPS})
    try:
        groups=json.loads(get('groupBy','["Service"]'));filters=json.loads(get('filter','[]'))
    except (ValueError,TypeError):raise ValueError('The console URL has invalid filter or grouping data.')
    if not isinstance(groups,list) or len(groups)>1:raise ValueError('Import one grouping dimension at a time.')
    group = str(groups[0]) if groups else ''
    if group.startswith('TagKeyValue:'):
        p['group_by'], p['group_key'] = 'tag', group.split(':', 1)[1]
    elif group.startswith('CostCategoryKeyValue:'):
        raise ValueError('This cost category grouping URL format has not been verified. Select the category key in this console.')
    else:
        p['group_by']=group_alias.get(canonical(group),'invalid') if groups else 'none'
    if not isinstance(filters,list) or len(filters)>len(FILTERS)+20:raise ValueError('The console URL has invalid filters.')
    additional = []
    for item in filters:
        if not isinstance(item,dict) or not isinstance(item.get('dimension'),dict):raise ValueError('Unsupported AWS filter format. No filters were imported.')
        key=aliases.get(canonical(str(item['dimension'].get('id',''))))
        mode={'INCLUDES':'include','EXCLUDES':'exclude'}.get(item.get('operator'))
        if key not in FILTERS or not mode or (key in p and key not in ('tag', 'cost_category')):
            raise ValueError('Unsupported or repeated AWS filter. Select this filter in the dashboard; no filters were discarded.')
        if key == 'cost_category':
            raise ValueError('This cost category filter URL format has not been verified. Select the category key in this console.')
        if key in ('tag', 'cost_category'):
            selected_key = item.get('growableValue', {}).get('value') if isinstance(item.get('growableValue'), dict) else None
            if not isinstance(selected_key, str) or not selected_key:
                raise ValueError('The AWS tag or cost category filter must include its key.')
        vals=item.get('values')
        if not isinstance(vals,list) or any(not isinstance(v,dict) or not isinstance(v.get('value'),str) for v in vals):raise ValueError('The console URL has invalid filter values.')
        selected_values = [v['value'] or EMPTY_VALUE for v in vals]
        if key in ('tag', 'cost_category') and key in p:
            additional.append({'type': key, 'key': selected_key, 'values': selected_values, 'mode': mode, 'absent': False})
        else:
            p[key]=selected_values;p[key+'_mode']=mode
            if key in ('tag', 'cost_category'):p[key+'_key']=selected_key
    p['keyed_filters'] = additional
    if get('usageAggregate','undefined') not in ('','undefined','null'):
        p['measure']='cost_usage' if get('costAggregate') else 'usage'
    return normalize(p, today=today)


def export_console_url(params, today=None):
    """Build an AWS console handoff with the complete representable report definition.

    Customer/source restrictions belong to the caller's authorized AWS account selection;
    this helper never serializes connection credentials or local customer identifiers.
    """
    p = normalize(params, today=today)
    for kind, flag in (('tag','untagged'),('cost_category','uncategorized')):
        if p[flag]=='1' and not p[kind+'_key'] and p[kind+'_mode']=='exclude':
            raise ValueError('Choose a specific tag or category key before opening an excluded absence filter in AWS.')
    if p['group_by'] == 'cost_category' or p['cost_category'] or (p['uncategorized']=='1' and p['cost_category_key']) or any(item['type']=='cost_category' and (item['values'] or item['absent']) for item in keyed_filters(p)):
        raise ValueError('The AWS cost category URL encoding has not been verified. Open AWS and select the category parameters there.')
    if p['group_by'] == 'customer':
        raise ValueError('Customer grouping is local to this console. Choose an AWS dimension to open this report in AWS.')
    if p['forecast']=='1' and date.fromisoformat(p['end'])>(today or timezone.now().date()) and p['group_by']!='none':
        raise ValueError('AWS forecasting requires Group by None. This console keeps grouped actuals and separate customer forecasts; choose None before opening the forecast in AWS.')
    names = {
        'service': 'Service', 'account': 'LinkedAccount', 'region': 'Region',
        'instance_type': 'InstanceType', 'usage_type': 'UsageType', 'usage_type_group': 'UsageTypeGroup',
        'resource': 'ResourceId', 'charge_type': 'RecordType', 'az': 'AZ', 'platform': 'Platform',
        'purchase_option': 'PurchaseType', 'tenancy': 'Tenancy', 'database_engine': 'DatabaseEngine',
        'legal_entity': 'LegalEntityName', 'billing_entity': 'BillingEntity', 'operation': 'Operation',
        'payer': 'PayerAccount', 'tag': 'TagKey', 'cost_category': 'CostCategoryKey',
    }
    group = p['group_by']
    groups = [] if group == 'none' else [
        'TagKeyValue:'+p['group_key'] if group == 'tag' else
        'CostCategoryKeyValue:'+p['group_key'] if group == 'cost_category' else names[group]
    ]
    filters = []
    for key, (label, _) in FILTERS.items():
        if not p[key]:
            continue
        item = {'dimension': {'id': names[key], 'displayValue': label},
                'operator': 'EXCLUDES' if p[key+'_mode'] == 'exclude' else 'INCLUDES',
                'values': [{'value': '' if value == EMPTY_VALUE else value,
                            'displayValue': ('No '+label.lower()+' key: '+p[key+'_key'])
                            if value == EMPTY_VALUE and key in ('tag', 'cost_category') else
                            '(Empty value)' if value == EMPTY_VALUE else value} for value in p[key]]}
        if key in ('tag', 'cost_category'):
            item['growableValue'] = {'value': p[key+'_key'], 'displayValue': p[key+'_key']}
        filters.append(item)
    for item in keyed_filters(p):
        if not item['values'] and not item['absent']:
            continue
        selected = [EMPTY_VALUE] if item['absent'] else item['values']
        filters.append({'dimension': {'id': names[item['type']], 'displayValue': FILTERS[item['type']][0]},
                        'operator': 'EXCLUDES' if item['mode']=='exclude' else 'INCLUDES',
                        'values': [{'value': '' if value==EMPTY_VALUE else value,
                                    'displayValue': 'No tag key: '+item['key'] if value==EMPTY_VALUE else value} for value in selected],
                        'growableValue': {'value': item['key'], 'displayValue': item['key']}})
    # Keyed absence cannot be expressed using the global absence switch alone.
    for key, flag in (('tag', 'untagged'), ('cost_category', 'uncategorized')):
        if p[flag] == '1' and p[key+'_key']:
            filters.append({'dimension': {'id': names[key], 'displayValue': FILTERS[key][0]},
                            'operator': 'EXCLUDES' if p[key+'_mode']=='exclude' else 'INCLUDES', 'values': [{'value': '', 'displayValue': 'No '+key+' key: '+p[key+'_key']}],
                            'growableValue': {'value': p[key+'_key'], 'displayValue': p[key+'_key']}})
    reverse = {value: key for key, value in reversed(list(HISTORICAL_CONSOLE_RANGES.items()))}
    # Current live console encodings; accept older aliases on import only.
    reverse.update(last_month='LAST_MONTH', last_12_months='LAST_12_MONTHS')
    query = {
        'reportMode': p['report_mode'].upper(), 'reportName': p['report_name'],
        'granularity': p['granularity'].title(), 'groupBy': json.dumps(groups, separators=(',', ':')),
        'chartStyle': {'stacked': 'STACK', 'line': 'LINE', 'bar': 'BAR'}[p['chart_style']],
        'costAggregate': {'unblended': 'unBlendedCost', 'amortized': 'amortizedCost',
                          'blended': 'blendedCost', 'net_unblended': 'netUnblendedCost',
                          'net_amortized': 'netAmortizedCost'}[p['metric']],
        'usageAggregate': 'usageQuantity' if p['measure'] in ('usage','cost_usage') else 'undefined',
        'excludeForecasting': str(p['forecast'] != '1').lower(),
        'showOnlyUntagged': str(p['untagged'] == '1' and not p['tag_key']).lower(),
        'showOnlyUncategorized': str(p['uncategorized'] == '1' and not p['cost_category_key']).lower(),
        'useNormalizedUnits': str(p['normalized'] == '1').lower(),
        'filter': json.dumps(filters, separators=(',', ':')),
    }
    if p['report_mode'] == 'compare':
        query.update(comparisonStartDate=p['start'], comparisonEndDate=p['end'],
                     baselineStartDate=p['compare_start'], baselineEndDate=p['compare_end'])
        if p['compare_range'] == 'month_over_month':
            query['compareRelativeRange'] = 'MONTH_OVER_MONTH'
    else:
        query.update(startDate=p['start'], endDate=p['end'])
        if p['date_range'] in reverse:
            query['historicalRelativeRange'] = reverse[p['date_range']]
        if p['future_range'] != 'none':
            query['futureRelativeRange'] = next(key for key, value in FUTURE_CONSOLE_RANGES.items() if value == p['future_range'])
    return 'https://us-east-1.console.aws.amazon.com/costmanagement/home#/cost-explorer?'+urlencode(query)
