"""Cost Explorer-style grouped reports built entirely from imported billing rows."""
from datetime import timedelta
from decimal import Decimal
from urllib.parse import urlencode
from django.db.models import Sum
from django.db.models.functions import TruncMonth
from django.utils import timezone

SERVICE_LABELS = {
    'Amazon Elastic Compute Cloud - Compute': 'EC2-Instances', 'EC2 - Other': 'EC2-Other',
    'Amazon Simple Storage Service': 'S3', 'Amazon Virtual Private Cloud': 'VPC',
    'AmazonCloudWatch': 'CloudWatch', 'Amazon EC2 Container Registry (ECR)': 'EC2 Container Registry (ECR)',
    'Amazon Simple Notification Service': 'SNS', 'Amazon Simple Queue Service': 'SQS',
    'Amazon Relational Database Service': 'RDS', 'Amazon Elastic Load Balancing': 'Elastic Load Balancing',
}
CHART_COLORS = ['#6483db', '#ba4265', '#38988e', '#8b60bd', '#d49436', '#4089a9', '#cc7053', '#697dba', '#81963a', '#828b98']


def defaults(params):
    result = params.copy()
    today = timezone.now().date()
    last_month = today.replace(day=1) - timedelta(days=1)
    first_month = last_month.replace(day=1)
    for _ in range(5):
        first_month = (first_month - timedelta(days=1)).replace(day=1)
    if not result.get('start') and not result.get('end'):
        result['start'], result['end'] = str(first_month), str(last_month)
    if not result.get('granularity'):
        result['granularity'] = 'monthly'
    return result


def explorer_report(context, params):
    group = params.get('group_by', 'service')
    style = params.get('chart_style', 'stacked')
    if group not in ('service', 'account', 'customer'):
        raise ValueError('Group costs by service, linked account or customer.')
    if style not in ('stacked', 'bar', 'line'):
        raise ValueError('Choose stacked bars, bars or lines for the chart.')
    dimension = {'service': 'service', 'account': 'account_id', 'customer': 'customer_id'}[group]
    group_label = {'service': 'Service', 'account': 'Linked account', 'customer': 'Customer'}[group]
    monthly = context['granularity'] == 'monthly'
    selected = context['selected']
    if monthly:
        query = selected.annotate(period=TruncMonth('day')).values(dimension, 'period').annotate(amount=Sum(context['metric'])).order_by()
    else:
        query = selected.values(dimension, 'day').annotate(amount=Sum(context['metric'])).order_by()
    customer_names = {row['customer'].pk: row['customer'].name for row in context['rows']}
    periods = [point['label'] for point in context['points']]
    values, totals = {}, {}
    for record in query:
        key = record[dimension]
        period = record['period'].strftime('%b %Y') if monthly else record['day'].isoformat()
        values.setdefault(key, {})[period] = record['amount']
        totals[key] = totals.get(key, Decimal(0)) + record['amount']
    filters = dict(start=str(context['start']), end=str(context['end']), currency=context['currency'],
                   metric=context['metric'], granularity=context['granularity'], customer=context['filter_customer'],
                   account=context['filter_account'], service=context['filter_service'], group_by=group, chart_style=style)

    def url(**changes):
        return '/?' + urlencode({k: v for k, v in (filters | changes).items() if v})

    def display(key):
        if group == 'customer':
            return customer_names[key]
        if group == 'service':
            return SERVICE_LABELS.get(key, key.removeprefix('AWS ').removeprefix('Amazon '))
        return key

    rows = []
    for key in sorted(totals, key=lambda key: (-totals[key], str(key))):
        cells = [values[key].get(period) for period in periods]
        rows.append({'key': str(key), 'label': display(key), 'source_label': display(key) if group == 'customer' else str(key), 'total': totals[key], 'cells': cells,
                     'url': url(**{group: str(key)})})
    # Top nine dimensions plus Others; retain all dimensions in the table/export.
    top = sorted(rows, key=lambda row: -sum(abs(value or 0) for value in row['cells']))[:9]
    top_keys = {row['key'] for row in top}
    remaining = [row for row in rows if row['key'] not in top_keys]
    series = [{'label': row['label'], 'values': [float(value) if value is not None else None for value in row['cells']],
               'color': CHART_COLORS[i]} for i, row in enumerate(top)]
    if remaining:
        cells = [sum((row['cells'][i] or Decimal(0)) for row in remaining) if any(row['cells'][i] is not None for row in remaining) else None for i in range(len(periods))]
        series.append({'label': 'Others', 'values': [float(v) if v is not None else None for v in cells], 'color': CHART_COLORS[-1]})
    period_totals = [sum((row['cells'][i] or Decimal(0)) for row in rows) if any(row['cells'][i] is not None for row in rows) else None for i in range(len(periods))]
    previous_month_end = context['today'].replace(day=1) - timedelta(days=1)
    presets = []
    for label, months in [('Last 3 months', 3), ('Last 6 months', 6)]:
        start = previous_month_end.replace(day=1)
        for _ in range(months-1):
            start = (start - timedelta(days=1)).replace(day=1)
        presets.append({'label': label, 'url': url(start=str(start), end=str(previous_month_end)), 'start': str(start), 'end': str(previous_month_end)})
    presets.insert(0, {'label': 'This month', 'url': url(start=str(context['today'].replace(day=1)), end=str(context['today'])), 'start': str(context['today'].replace(day=1)), 'end': str(context['today'])})
    context.update({'group_by': group, 'group_label': group_label, 'chart_style': style,
                    'pivot_rows': rows, 'periods': periods, 'period_totals': period_totals,
                    'group_count': len(rows), 'period_average': context['total'] / len(periods) if rows and periods else None,
                    'chart_payload': {'periods': periods, 'series': series, 'totals': [float(v) if v is not None else None for v in period_totals], 'currency': context['currency'], 'style': style},
                    'chart_series': series, 'export_query': urlencode({k: v for k, v in filters.items() if v}),
                    'report_presets': presets,
                    'parameter_fields': filters,
                    'style_options': [{'label': label, 'value': value, 'url': url(chart_style=value)} for value, label in [('bar','Bar'), ('line','Line'), ('stacked','Stacked')]],
                    'active_page': 'explorer'})
    return context
