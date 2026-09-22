"""Saved report management within the request's current customer authorization."""
from copy import deepcopy
from urllib.parse import urlencode
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from . import scope as scoping
from . import parameters as contract
from .access import editing
from .models import SavedReport
from .web import audit, paginate, staff_required


class ReportNameForm(forms.Form):
    name = forms.CharField(max_length=120)


LIBRARY_COLUMNS = [
    ('name', 'Report name'), ('report_type', 'Type'), ('time_range', 'Time range'),
    ('granularity', 'Time granularity'), ('group_by', 'Grouped by'), ('filter_count', 'Filtered by'),
]


@never_cache
@login_required
def new_report(request):
    aws = 'https://us-east-1.console.aws.amazon.com/costmanagement/home#/'
    types = [
        ('Savings Plans utilization', 'savings-plans/utilization'),
        ('Savings Plans coverage', 'savings-plans/coverage'),
        ('Reservation utilization', 'ri/utilization'),
        ('Reservation coverage', 'ri/coverage'),
    ]
    return render(request, 'billing/new_report.html', {
        'active_page': 'explorer',
        'aws_report_types': [{'label': label, 'url': aws+path} for label, path in types],
    })


def report_summary(saved):
    """Describe saved settings without refreshing costs or resolving AWS metadata."""
    p = saved.parameters if isinstance(saved.parameters, dict) else {}
    time_range = contract.DATE_RANGES.get(p.get('date_range'), '')
    if not time_range or p.get('date_range') == 'custom':
        time_range = f'{p["start"]} – {p["end"]}' if p.get('start') and p.get('end') else 'Last 6 months'
    if p.get('report_mode') == 'compare':
        if p.get('compare_range') == 'month_over_month':
            time_range = 'Month over month'
        elif p.get('compare_range') == 'previous_period':
            time_range += ' vs preceding period'
        elif p.get('compare_start') and p.get('compare_end'):
            time_range += f' vs {p["compare_start"]} – {p["compare_end"]}'
    elif p.get('future_range') in contract.FUTURE_RANGES and p['future_range'] != 'none':
        time_range += ' + ' + contract.FUTURE_RANGES[p['future_range']]
    group = contract.GROUPS.get(p.get('group_by', 'service'), 'Unknown grouping')
    if p.get('group_by') in ('tag', 'cost_category') and p.get('group_key'):
        group += ': ' + str(p['group_key'])
    filters = [label for key, (label, _) in contract.FILTERS.items() if p.get(key)]
    for key, label in [('untagged', 'Tag'), ('uncategorized', 'Cost category')]:
        if contract.truth(p.get(key, '0')) and label not in filters:
            filters.append(label)
    filter_warning = ''
    try:
        additional_filters = contract.keyed_filters(p)
    except ValueError:
        additional_filters = []
        filter_warning = 'Additional filters need review.'
    for item in additional_filters:
        if item['values'] or item['absent']:
            filters.append('Tag' if item.get('type') == 'tag' else 'Cost category')
    return {
        'name': saved.name,
        'report_type': ('Usage comparison' if p.get('measure') == 'usage' else 'Cost comparison') if p.get('report_mode') == 'compare' else 'Cost and usage',
        'time_range': time_range, 'granularity': str(p.get('granularity', 'monthly')).capitalize(),
        'group_by': group, 'filter_count': len(filters), 'filter_labels': ', '.join(filters) or 'None',
        'filter_warning': filter_warning,
    }


@never_cache
@login_required
def library(request):
    archived = request.GET.get('status') == 'archived'
    query = request.GET.get('q', '').strip()[:120]
    reports = SavedReport.objects.select_related('customer')
    active_count = reports.filter(archived_at__isnull=True).count()
    archived_count = reports.filter(archived_at__isnull=False).count()
    selected = reports.filter(archived_at__isnull=not archived)
    if query:
        selected = selected.filter(name__icontains=query)
    sort = request.GET.get('sort', 'name')
    if sort not in dict(LIBRARY_COLUMNS):
        sort = 'name'
    descending = request.GET.get('dir') == 'desc'
    selected = list(selected)
    for row in selected:
        row.summary = report_summary(row)
    selected.sort(key=lambda row: row.summary[sort] if sort == 'filter_count' else row.summary[sort].casefold(), reverse=descending)
    page = paginate(request, selected)
    rows = list(page.object_list)
    can_edit = scoping.can_edit(request.user)
    editable_ids = set()
    if can_edit:
        # A user can edit one customer and only read another. Apply the same
        # write scope here as the mutation endpoints, including creator ownership.
        with editing():
            editable_ids = set(SavedReport.objects.filter(pk__in=[row.pk for row in rows]).values_list('pk', flat=True))
    for row in rows:
        row.can_edit = row.pk in editable_ids
    query_params = {'q': query, **({'status': 'archived'} if archived else {})}
    headers = [{'label': label, 'url': '?' + urlencode({**query_params, 'sort': key, 'dir': 'desc' if sort == key and not descending else 'asc'}),
                'direction': ('descending' if descending else 'ascending') if key == sort else None} for key, label in LIBRARY_COLUMNS]
    return render(request, 'billing/report_library.html', {
        'active_page': 'explorer', 'reports': rows, 'page': page,
        'archived': archived, 'query': query, 'can_edit': can_edit,
        'active_count': active_count, 'archived_count': archived_count,
        'headers': headers, 'sort': sort, 'direction': 'desc' if descending else 'asc',
        'pagination_query': urlencode({**query_params, 'sort': sort, 'dir': 'desc' if descending else 'asc'}),
    })


def copy_report(request, saved):
    if not isinstance(saved.parameters, dict):
        raise ValueError('This saved report has an invalid definition.')
    name = saved.name[:113] + ' (copy)'
    parameters = {**deepcopy(saved.parameters), 'report_name': name}
    copied = SavedReport.objects.create(customer=saved.customer, name=name, parameters=parameters, created_by=request.user.username)
    audit(request, 'Explorer report duplicated', customer=saved.customer, report_id=copied.pk, source_report_id=saved.pk)
    return copied


@require_POST
@staff_required
@transaction.atomic
def duplicate_report(request, pk):
    saved = get_object_or_404(SavedReport.objects.select_for_update(), pk=pk)
    try:
        copied = copy_report(request, saved)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    messages.success(request, 'Report duplicated. The original report is unchanged.')
    return redirect('open_report', pk=copied.pk)


@require_POST
@staff_required
@transaction.atomic
def selected_reports(request):
    action = request.POST.get('action')
    try:
        ids = {int(value) for value in request.POST.getlist('report_ids')}
    except (ValueError, TypeError):
        return HttpResponseBadRequest('Select valid reports.')
    if not ids or len(ids) > 100 or action not in ('archive', 'restore', 'duplicate'):
        return HttpResponseBadRequest('Select 1–100 reports and a valid action.')
    reports = list(SavedReport.objects.select_for_update().filter(pk__in=ids))
    if len(reports) != len(ids):
        raise Http404('A selected report is unavailable.')
    # Validate the entire selection before making any writes.
    if action == 'duplicate' and any(not isinstance(row.parameters, dict) for row in reports):
        return HttpResponseBadRequest('A selected report has an invalid definition.')
    for saved in reports:
        if action == 'duplicate':
            copy_report(request, saved)
        elif action == 'archive' and saved.archived_at is None:
            saved.archived_at = timezone.now()
            saved.save(update_fields=['archived_at'])
            audit(request, 'Explorer report archived', customer=saved.customer, report_id=saved.pk)
        elif action == 'restore' and saved.archived_at is not None:
            saved.archived_at = None
            saved.save(update_fields=['archived_at'])
            audit(request, 'Explorer report restored', customer=saved.customer, report_id=saved.pk)
    verb = {'archive': 'archived', 'restore': 'restored', 'duplicate': 'duplicated'}[action]
    messages.success(request, f'{len(reports)} report(s) {verb}.')
    return redirect('/reports/?status=archived' if action == 'archive' else '/reports/')


@require_POST
@staff_required
@transaction.atomic
def rename_report(request, pk):
    saved = get_object_or_404(SavedReport.objects.select_for_update(), pk=pk)
    form = ReportNameForm(request.POST)
    if not form.is_valid():
        return HttpResponseBadRequest('Enter a report name between 1 and 120 characters.')
    name = form.cleaned_data['name']
    if not isinstance(saved.parameters, dict):
        return HttpResponseBadRequest('This saved report has an invalid definition.')
    saved.name = name
    saved.parameters = {**saved.parameters, 'report_name': name}
    saved.save(update_fields=['name', 'parameters'])
    audit(request, 'Explorer report renamed', customer=saved.customer, report_id=saved.pk)
    messages.success(request, 'Report renamed.')
    return redirect('/reports/?status=archived' if saved.archived_at else '/reports/')


@require_POST
@staff_required
@transaction.atomic
def archive_report(request, pk):
    saved = get_object_or_404(SavedReport.objects.select_for_update(), pk=pk)
    if saved.archived_at is None:
        saved.archived_at = timezone.now()
        saved.save(update_fields=['archived_at'])
        audit(request, 'Explorer report archived', customer=saved.customer, report_id=saved.pk)
    messages.success(request, 'Report archived. You can restore it from Archived reports.')
    return redirect('/reports/?status=archived')


@require_POST
@staff_required
@transaction.atomic
def restore_report(request, pk):
    saved = get_object_or_404(SavedReport.objects.select_for_update(), pk=pk)
    if saved.archived_at is not None:
        saved.archived_at = None
        saved.save(update_fields=['archived_at'])
        audit(request, 'Explorer report restored', customer=saved.customer, report_id=saved.pk)
    messages.success(request, 'Report restored to the active library.')
    return redirect('report_library')
