"""Optional AWS-native change explanations; core comparison never depends on this API."""
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from dateutil.relativedelta import relativedelta
from . import parameters, scope
from .query_cache import get_query


def build(p, units):
    rows=[];queries=[];notes=[]
    before=date.fromisoformat(p['compare_start']);after=date.fromisoformat(p['start'])
    before_end=date.fromisoformat(p['compare_end'])+timedelta(days=1)
    after_end=date.fromisoformat(p['end'])+timedelta(days=1)
    if before.day!=1 or after.day!=1 or before_end!=before+relativedelta(months=1) or after_end!=after+relativedelta(months=1):
        return {'rows':[],'queries':[],'notes':['AWS cost comparison drivers require two complete calendar months. Use Month over month or select two full months.']}
    for source,customer,accounts in units:
        label=(customer or source.customer).name
        if not source.capabilities.get('comparison_drivers'):
            notes.append(f'{label}: AWS explanations require customer approval and ce:GetCostComparisonDrivers on the existing billing role.')
            continue
        left=scope.ownership_windows(source,customer,before,before_end)
        right=scope.ownership_windows(source,customer,after,after_end)
        if len(left)!=1 or len(right)!=1 or left[0][:2]!=(before,before_end) or right[0][:2]!=(after,after_end) or left[0][2]!=right[0][2]:
            notes.append(f'{label}: account ownership changed during these months. Cost totals remain scoped by date; AWS driver analysis requires a stable account scope.')
            continue
        owned=left[0][2]
        selected=sorted(set(owned)&set(accounts)) if owned is not None and accounts is not None else owned if owned is not None else accounts
        _,standard=parameters.aws_request(p)
        request={'BaselineTimePeriod':{'Start':str(before),'End':str(before_end)},'ComparisonTimePeriod':{'Start':str(after),'End':str(after_end)},'MetricForComparison':standard['Metrics'][0],'MaxResults':10}
        if standard.get('Filter'):request['Filter']=standard['Filter']
        if standard.get('GroupBy'):request['GroupBy']=standard['GroupBy']
        try:q=get_query(source,'get_cost_comparison_drivers',request,customer=customer,account_filter=selected)
        except ValueError as exc:
            notes.append(f'{label}: {exc}');continue
        queries.append(q)
        if q.error:notes.append(f'{label}: {q.error}')
        if q.data is not None and not q.data.get('CostComparisonDrivers') and not q.error:
            notes.append(f'{label}: AWS returned no cost comparison drivers for this selection.')
        try:rows.extend(parse_rows(q.data or {},label))
        except (ValueError,TypeError,KeyError,InvalidOperation):
            notes.append(f'{label}: AWS comparison driver data could not be interpreted. Cost totals remain available.')
    return {'rows':rows,'queries':queries,'notes':notes}


def parse_rows(data,label):
    rows=[]
    for item in data.get('CostComparisonDrivers',[]):
        dimensions=[]
        def describe(expression):
            for kind,term in expression.items():
                if kind in ('And','Or'):
                    for child in term:describe(child)
                elif kind=='Not':continue
                elif kind in ('Dimensions','Tags','CostCategories'):
                    dimensions.extend(term.get('Values',[]))
        describe(item.get('CostSelector',{}))
        facts=[]
        for driver in item.get('CostDrivers',[]):
            for metric,values in driver.get('Metrics',{}).items():
                amounts=[Decimal(values[k]) for k in ('BaselineTimePeriodAmount','ComparisonTimePeriodAmount','Difference')]
                if not all(v.is_finite() for v in amounts):raise ValueError('AWS returned a non-finite comparison driver.')
                facts.append({'name':driver.get('Name') or driver.get('Type') or metric,'metric':metric,'before':amounts[0],'after':amounts[1],'delta':amounts[2],'unit':values.get('Unit','')})
        rows.append({'customer':label,'label':' · '.join(dimensions) or 'Selected costs','facts':facts})
    return rows
