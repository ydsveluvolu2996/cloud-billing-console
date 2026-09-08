"""Validated report contract shared by HTML, CSV, saved reports and the worker."""
import json
from datetime import date, timedelta
from urllib.parse import parse_qs, urlsplit
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


def truth(value):
    return str(value).lower() in ('true', '1', 'on')


def values(params, key):
    raw = params.getlist(key) if hasattr(params, 'getlist') else params.get(key, [])
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or any(not isinstance(v, str) or len(v)>1024 for v in raw) or len(raw)>100:
        raise ValueError('Each filter supports up to 100 values of at most 1,024 characters.')
    return sorted(set(v for v in raw if v != ''))


def normalize(params, today=None):
    today = today or timezone.now().date()
    p = {k:params.get(k, '') for k in ('customer','currency','start','end','granularity','group_by','group_key','chart_style','metric','report_mode','compare_start','compare_end','tag_key','cost_category_key','report_name','date_range','measure','forecast','untagged','uncategorized','normalized')}
    p.update(currency=p['currency'] or 'USD', granularity=p['granularity'] or 'monthly', group_by=p['group_by'] or 'service', chart_style=p['chart_style'] or 'stacked', metric=p['metric'] or 'unblended', report_mode=p['report_mode'] or 'standard', measure=p['measure'] or 'cost', report_name=p['report_name'] or 'Cost report')
    p['date_range'] = p['date_range'] or ('custom' if p['start'] and p['end'] else 'last_6_months')
    month = today.replace(day=1)
    if p['date_range'].startswith('last_') and p['date_range'] in ('last_3_months','last_6_months','last_12_months'):
        months = int(p['date_range'].split('_')[1]); p['start'] = str(month-relativedelta(months=months)); p['end'] = str(month-timedelta(days=1))
    elif p['date_range'] in ('this_month','last_month','last_7_days','last_14_days'):
        ranges={'this_month':(month,today), 'last_month':(month-relativedelta(months=1),month-timedelta(days=1)), 'last_7_days':(today-timedelta(days=6),today), 'last_14_days':(today-timedelta(days=13),today)}
        p['start'],p['end']=map(str,ranges[p['date_range']])
    elif p['date_range'] != 'custom':
        raise ValueError('Choose a valid date range.')
    try:
        start,end=date.fromisoformat(p['start']),date.fromisoformat(p['end'])
    except (ValueError,TypeError):
        raise ValueError('Choose valid start and end dates.')
    if start>end or start>today or (end-start).days>731 or end>today+relativedelta(months=18):
        raise ValueError('Choose a range up to two years, starting today or earlier, with forecasts up to 18 months ahead.')
    if p['granularity'] not in ('daily','monthly','hourly') or p['group_by'] not in GROUPS or p['chart_style'] not in ('stacked','line','bar') or p['metric'] not in METRICS or p['report_mode'] not in ('standard','compare') or p['measure'] not in ('cost','usage'):
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
    if p['group_by'] in ('tag','cost_category') and not p['group_key']:
        raise ValueError('Choose a tag or cost category key to group by.')
    for key,flag in [('tag','untagged'),('cost_category','uncategorized')]:
        if p[key] and not p[key+'_key']:
            raise ValueError(f'Choose the {FILTERS[key][0].lower()} key for this filter.')
        if p[key] and p[flag]=='1': raise ValueError('Clear the selected values before choosing only untagged or uncategorized resources.')
    if p['measure']=='usage' and not (len(p['usage_type'])==1 and p['usage_type_mode']=='include'):
        raise ValueError('Select one usage type before viewing usage so different units are not added together.')
    if p['normalized']=='1' and p['measure']!='usage':
        raise ValueError('Choose Usage quantity to view normalized units.')
    resource = p['resource'] or p['group_by']=='resource'
    if resource and (p['service'] != [EC2] or p['service_mode']!='include'):
        raise ValueError('Resource reports require Service = Amazon Elastic Compute Cloud - Compute (EC2-Instances).')
    if (resource or p['granularity']=='hourly') and (start<today-timedelta(days=13) or end>today):
        raise ValueError('Resource and hourly reports require dates within the last 14 days and enabled AWS granular data.')
    if p['report_mode']=='compare':
        if not p['compare_start'] or not p['compare_end']:
            if start.day==1 and (end+timedelta(days=1)).day==1:
                months=(end.year-start.year)*12+end.month-start.month+1
                p['compare_start']=str(start-relativedelta(months=months));p['compare_end']=str(start-timedelta(days=1))
            else:
                p['compare_start']=str(start-timedelta(days=(end-start).days+1));p['compare_end']=str(start-timedelta(days=1))
        try: cs,ce=date.fromisoformat(p['compare_start']),date.fromisoformat(p['compare_end'])
        except (ValueError,TypeError): raise ValueError('Choose valid comparison dates.')
        if cs>ce or ce>today or (ce-cs).days>731: raise ValueError('Comparison must cover up to two years ending today or earlier.')
        if (resource or p['granularity']=='hourly') and cs<today-timedelta(days=13): raise ValueError('Comparison must also be within the last 14 days for resource/hourly reports.')
    if end>today:
        if p['forecast']!='1' or p['measure']!='cost' or p['report_mode']=='compare': raise ValueError('Future dates require forecasted costs in Standard mode.')
        if p['granularity']=='daily' and end>today+relativedelta(months=3): raise ValueError('Daily forecasts cover up to three months.')
    return p


def expression(p):
    parts=[]
    for key,(_,aws_key) in FILTERS.items():
        if key in ('tag','cost_category'):
            category='Tags' if key=='tag' else 'CostCategories'
            if p['untagged' if key=='tag' else 'uncategorized']=='1':
                absent={'MatchOptions':['ABSENT']}
                if p[key+'_key']:absent['Key']=p[key+'_key']
                parts.append({category:absent});continue
            expr={category:{'Key':p[key+'_key'],'Values':['' if v==EMPTY_VALUE else v for v in p[key]]}}
        else:
            expr={'Dimensions':{'Key':aws_key,'Values':['' if v==EMPTY_VALUE else v for v in p[key]]}}
        if p[key]: parts.append({'Not':expr} if p[key+'_mode']=='exclude' else expr)
    return {'And':parts} if len(parts)>1 else parts[0] if parts else None


def aws_request(p, start=None, end=None):
    start=start or p['start']; end=end or p['end']
    metric = ('NormalizedUsageAmount' if p['normalized']=='1' else 'UsageQuantity') if p['measure']=='usage' else METRICS[p['metric']][1]
    req={'TimePeriod':{'Start':start,'End':str(date.fromisoformat(end)+timedelta(days=1))},'Granularity':p['granularity'].upper(),'Metrics':[metric]}
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


def import_console_url(url):
    """Import documented console link fields without fetching an arbitrary URL."""
    parsed=urlsplit(url)
    if parsed.scheme!='https' or not parsed.hostname or not parsed.hostname.endswith('.console.aws.amazon.com') or 'cost-explorer?' not in parsed.fragment:
        raise ValueError('Paste an HTTPS AWS Cost Explorer report URL.')
    raw=parse_qs(parsed.fragment.split('?',1)[1],keep_blank_values=True)
    get=lambda key,default='':raw.get(key,[default])[0]
    p={'start':get('startDate'),'end':get('endDate'),'date_range':'custom',
       'granularity':get('granularity','Monthly').lower(),'report_mode':get('reportMode','STANDARD').lower(),
       'report_name':get('reportName','Cost report'),'chart_style':{'STACK':'stacked','BAR':'bar','LINE':'line'}.get(get('chartStyle','STACK'),'invalid'),
       'metric':{'unBlendedCost':'unblended','amortizedCost':'amortized','blendedCost':'blended','netUnblendedCost':'net_unblended','netAmortizedCost':'net_amortized'}.get(get('costAggregate','unBlendedCost'),'invalid'),
       'forecast':'0' if truth(get('excludeForecasting','false')) else '1','untagged':get('showOnlyUntagged','false'),
       'uncategorized':get('showOnlyUncategorized','false'),'normalized':get('useNormalizedUnits','false')}
    group_alias={label.lower().replace(' ',''):key for key,label in GROUPS.items()}
    try:
        groups=json.loads(get('groupBy','["Service"]'));filters=json.loads(get('filter','[]'))
    except (ValueError,TypeError):raise ValueError('The console URL has invalid filter or grouping data.')
    if not isinstance(groups,list) or len(groups)>1:raise ValueError('Import one grouping dimension at a time.')
    p['group_by']=group_alias.get(str(groups[0]).lower().replace(' ',''),'invalid') if groups else 'none'
    if filters:
        raise ValueError('This console link contains encoded filters. Import an unfiltered link, then select the same filters in this dashboard; none have been silently discarded.')
    if get('usageAggregate','undefined') not in ('','undefined','null'):p['measure']='usage'
    return normalize(p)
