"""Report navigation and a deliberately scoped handoff to the AWS console."""
from datetime import date, timedelta

from django.conf import settings

from . import parameters as contract
from . import scope as scoping
from .access import current_access, for_user, scope_fingerprint


def aws_handoff(params):
    """Export only reports representable in one authorized AWS billing connection.

    AWS URLs cannot express time-varying ownership. Refuse those reports instead
    of losing the date-specific customer boundary when building a console link.
    The destination still uses the user's separate AWS console permissions.
    """
    p = contract.normalize(params)
    scope = scoping.resolve(p)
    if not scope.customer and not scope.source:
        raise ValueError('Choose one customer or billing connection to open this report in AWS.')
    start, end = date.fromisoformat(p['start']), date.fromisoformat(p['end']) + timedelta(days=1)
    if p['report_mode'] == 'compare':
        start = min(start, date.fromisoformat(p['compare_start']))
        end = max(end, date.fromisoformat(p['compare_end']) + timedelta(days=1))
    units = scoping.report_units(scope.customer, start=start, end=end)
    if scope.source:
        units = [unit for unit in units if unit[0].pk == scope.source.pk]
    if len(units) != 1:
        raise ValueError('Choose a report with one billing connection before opening AWS.')
    source, customer, accounts = units[0]
    if customer:
        windows = scoping.ownership_windows(source, customer, start, end)
        if (not windows or windows[0][0] != start or windows[-1][1] != end
                or any(left[1] != right[0] or left[2] != right[2]
                       for left, right in zip(windows, windows[1:]))):
            raise ValueError('This report spans changing account ownership. Keep this report in the billing console or choose dates with one ownership scope.')
        accounts = windows[0][2]
    else:
        # A tenant-restricted source-only view may have several customer units.
        # The caller's resolved report units, not source metadata, set its scope.
        access = current_access.get()
        if settings.ENFORCE_CUSTOMER_AUTHORIZATION and access and not access.portfolio and accounts is None:
            raise ValueError('Choose a customer before opening this report in AWS.')
    if accounts is not None:
        selected = set(p['account'])
        permitted = set(accounts)
        permitted = (permitted & selected if p['account_mode'] == 'include' else permitted - selected) if selected else permitted
        if not permitted:
            raise ValueError('No authorized accounts match this report.')
        if len(permitted) > 100:
            raise ValueError('Choose at most 100 accounts for an AWS console handoff.')
        p = p | {'account': sorted(permitted), 'account_mode': 'include'}
    return contract.export_console_url(p)


def _quick_reports(params):
    # Preserve customer/connection/account boundaries, including exclusions.
    base = {k: params[k] for k in ('customer', 'source', 'account', 'account_mode', 'currency')}
    base.update(forecast='0', future_range='none', metric=params['metric'], group_by='service', granularity='monthly')
    presets = [
        ('Top services last month', {'date_range': 'last_month'}),
        ('Projected cost next month', {'date_range': 'this_month', 'future_range': 'next_1_month', 'forecast': '1', 'group_by': 'none'}),
        ('Monthly spend for the past 3 months', {'date_range': 'last_3_months'}),
        ('Database costs this month · RDS and DynamoDB', {'date_range': 'this_month', 'service': ['Amazon Relational Database Service', 'Amazon DynamoDB']}),
        ('Service cost changes · last 2 complete months', {'date_range': 'last_month', 'report_mode': 'compare'}),
        ('Compute costs last month · EC2', {'date_range': 'last_month', 'service': [contract.EC2]}),
        ('Compare EC2 costs · last 2 complete months', {'date_range': 'last_month', 'report_mode': 'compare', 'service': [contract.EC2]}),
    ]
    reports = []
    for label, changes in presets:
        p = contract.normalize(base | changes | {'report_name': label})
        reports.append({'label': label, 'url': '/?' + contract.querydict(p).urlencode()})
    return reports


def workspace_context(request, report):
    p = report['params']
    access = current_access.get() or for_user(request.user)
    fingerprint = scope_fingerprint(access)
    history = request.session.get('explorer_recent', {})
    entries = history.get('reports', []) if history.get('scope') == fingerprint else []
    valid = []
    for entry in entries[:8]:
        try:
            old = contract.normalize(entry['parameters'])
            scoping.resolve(old)
        except (ValueError, KeyError, TypeError):
            continue
        valid.append({'name': old['report_name'], 'url': '/?' + contract.querydict(old).urlencode(), 'parameters': old})
    query = contract.querydict(p).urlencode()
    current_url = '/?' + query
    recent = [entry for entry in valid if entry['url'] != current_url]
    if len(query) <= 10000:
        request.session['explorer_recent'] = {'scope': fingerprint, 'reports':
            [{'parameters': entry['parameters']} for entry in ([{'parameters': p}] + recent)[:8]]}
    try:
        console_url, unavailable = aws_handoff(p), ''
    except ValueError as exc:
        console_url, unavailable = '', str(exc)
    can_save = scoping.can_edit(request.user)
    if settings.ENFORCE_CUSTOMER_AUTHORIZATION and not access.portfolio:
        can_save = can_save and p['customer'] in {str(cid) for cid in access.editable}
    return {
        'report_scope_id': fingerprint,
        'can_save_report': can_save,
        'recent_reports': recent,
        'report_library_url': '/reports/',
        'aws_console_url': console_url,
        'aws_console_unavailable': unavailable,
        'quick_reports': _quick_reports(p),
    }
