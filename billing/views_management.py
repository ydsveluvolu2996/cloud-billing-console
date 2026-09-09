"""Customer, connection, account, project, budget and onboarding management views."""
import csv
from datetime import date, timedelta
from decimal import Decimal
from dateutil.relativedelta import relativedelta
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Count, Prefetch, Q, Sum
from django.db.models.functions import TruncMonth
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST
from . import allocation, budgets as budgeting, onboarding, scheduler
from . import scope as scoping
from .collector import ensure_assignment
from .forms import (AllocationRuleForm, AssignmentForm, BudgetForm, BudgetOverrideForm, ConnectionForm, CsvUploadForm, CustomerForm, ProjectForm, SourceForm)
from .models import (AccountAssignment, Alert, AwsAccount, BillingSource, Budget, BudgetAmount, BudgetEvaluation, BulkImport, CollectionPeriod, Cost,
                     Customer, ImportedBudget, Job, Project, ProjectCost)
from .web import audit, paginate, staff_required

STATES = ['Awaiting customer setup', 'Connection verified', 'Account discovery complete', 'Initial import running', 'Connected',
          'Partial data', 'Permission problem', 'Stale data', 'Paused']


def month_param(request):
    try:
        return date.fromisoformat(request.GET.get('month', '')).replace(day=1)
    except ValueError:
        return timezone.now().date().replace(day=1)


def sort_param(request, allowed, default):
    key = request.GET.get('sort', default)
    direction = request.GET.get('dir', 'asc')
    if key not in allowed:
        key = default
    return key, 'desc' if direction == 'desc' else 'asc'


# --- overview & customers -----------------------------------------------------------------------

@never_cache
@login_required
def overview(request):
    today = timezone.now().date()
    month = today.replace(day=1)
    currency = request.GET.get('currency', 'USD')
    customers = Customer.objects.filter(active=True).prefetch_related('sources__periods')
    statuses = [c.status for c in customers]
    spend = Cost.objects.filter(customer__isnull=False, currency=currency, day__gte=month, day__lte=today).aggregate(v=Sum('unblended'))['v']
    last_month = month - relativedelta(months=1)
    previous = Cost.objects.filter(customer__isnull=False, currency=currency, day__gte=last_month, day__lt=month).aggregate(v=Sum('unblended'))['v']
    summary = budgeting.portfolio_summary(month)
    sources = BillingSource.objects.filter(customer__active=True, kind__in=[BillingSource.PAYER, BillingSource.STANDALONE]).select_related('customer').prefetch_related('periods')
    problem_sources = [s for s in sources if s.state in ('Partial data', 'Permission problem', 'Stale data')]
    top = list(Cost.objects.filter(customer__isnull=False, currency=currency, day__gte=month, day__lte=today).values('customer_id', 'customer__name').annotate(v=Sum('unblended')).order_by('-v')[:8])
    return render(request, 'billing/overview.html', {
        'active_page': 'overview', 'today': today, 'month': month, 'currency': currency,
        'customer_count': len(statuses), 'account_count': AwsAccount.objects.filter(assignments__end__isnull=True).distinct().count(),
        'source_count': sources.count(), 'connected_count': statuses.count('Connected'),
        'spend': spend, 'previous': previous, 'summary': summary, 'problem_sources': problem_sources,
        'unassigned_count': scoping.unassigned_accounts().count(),
        'open_alerts': Alert.objects.filter(acknowledged_at__isnull=True).count(),
        'setup_count': sum(s in ('Awaiting setup', 'Awaiting customer setup', 'Connection verified', 'Account discovery complete') for s in statuses),
        'importing_count': statuses.count('Initial import running'), 'top': top,
        'queue': scheduler.queue_summary(), 'failed_jobs': Job.objects.filter(status=Job.FAILED).count(),
        'currencies': sorted(set(Cost.objects.values_list('currency', flat=True).distinct()) | {'USD'})})


@never_cache
@login_required
def customers(request):
    today = timezone.now().date()
    month = today.replace(day=1)
    query = request.GET.get('q', '').strip()
    status_filter = request.GET.get('status', '')
    show = request.GET.get('show', 'active')
    items = Customer.objects.prefetch_related('sources__periods', Prefetch('budgets', queryset=Budget.objects.filter(active=True, scope=Budget.CUSTOMER).prefetch_related('amounts')))
    if show == 'active':
        items = items.filter(active=True)
    elif show == 'offboarded':
        items = items.filter(active=False)
    if query:
        items = items.filter(Q(name__icontains=query) | Q(reference__icontains=query) | Q(owner__icontains=query) | Q(sources__account_id__icontains=query) | Q(assignments__account__account_id__icontains=query)).distinct()
    mtd = {r['customer_id']: r['v'] for r in Cost.objects.filter(customer__in=items, currency='USD', day__gte=month, day__lte=today).values('customer_id').annotate(v=Sum('unblended'))}
    accounts = {r['customer_id']: r['n'] for r in AccountAssignment.objects.filter(customer__in=items, end__isnull=True).values('customer_id').annotate(n=Count('account_id', distinct=True))}
    evaluations = {e.budget.customer_id: e for e in BudgetEvaluation.objects.filter(month=month, budget__scope=Budget.CUSTOMER, budget__active=True, budget__customer__in=items).select_related('budget')}
    rows = []
    for c in items:
        status = c.status
        if status_filter and status != status_filter:
            continue
        budget = next(iter(c.budgets.all()), None)
        rows.append({'customer': c, 'status': status, 'mtd': mtd.get(c.pk), 'accounts': accounts.get(c.pk, 0), 'sources': len(c.cost_sources),
                     'budget': budget.amount_for(month) if budget else None, 'evaluation': evaluations.get(c.pk), 'last_success': c.last_success})
    sort, direction = sort_param(request, ['name', 'mtd', 'accounts', 'status', 'last_success'], 'name')
    keyfn = {'name': lambda r: r['customer'].name.lower(), 'mtd': lambda r: (r['mtd'] is None, r['mtd'] or 0), 'accounts': lambda r: r['accounts'],
             'status': lambda r: r['status'], 'last_success': lambda r: (r['last_success'] is None, r['last_success'] or timezone.now())}[sort]
    rows.sort(key=keyfn, reverse=direction == 'desc')
    page = paginate(request, rows, 25)
    all_statuses = [r['status'] for r in rows]
    return render(request, 'billing/customers.html', {
        'page': page, 'q': query, 'status_filter': status_filter, 'show': show, 'sort': sort, 'dir': direction, 'active_page': 'customers', 'month': month,
        'connected_count': all_statuses.count('Connected'), 'attention_count': sum(s in ('Stale data', 'Permission problem', 'Partial data', 'Awaiting setup', 'Awaiting customer setup') for s in all_statuses),
        'paused_count': all_statuses.count('Paused'), 'states': ['Awaiting setup'] + STATES,
        'query_string': '&'.join(f'{k}={v}' for k, v in request.GET.items() if k not in ('page', 'sort', 'dir') and v)})


@staff_required
def customer_add(request):
    form = CustomerForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        customer = form.save()
        if form.cleaned_data.get('budget'):
            budget = Budget.objects.create(customer=customer, scope=Budget.CUSTOMER, name=f'{customer.name} monthly budget', currency=customer.currency, created_by=request.user.username)
            BudgetAmount.objects.create(budget=budget, amount=form.cleaned_data['budget'], effective_from=timezone.now().date().replace(day=1))
        audit(request, 'Customer added', customer=customer)
        messages.success(request, 'Customer created. Add the management/payer or standalone account connection next.')
        return redirect('source_add', pk=customer.pk)
    return render(request, 'billing/customer_add.html', {'form': form, 'active_page': 'customers', 'steps': onboarding.WIZARD_STEPS, 'step': 1})


@never_cache
@login_required
def customer_detail(request, pk):
    customer = get_object_or_404(Customer.objects.prefetch_related('sources'), pk=pk)
    tab = request.GET.get('tab', 'accounts')
    if tab not in ('accounts', 'projects', 'budgets', 'reports', 'sync'):
        tab = 'accounts'
    today = timezone.now().date()
    month = month_param(request)
    currency = request.GET.get('currency', customer.currency or 'USD')
    context = {'customer': customer, 'tab': tab, 'active_page': 'customers', 'today': today, 'month': month, 'currency': currency,
               'sources': list(customer.sources.all()), 'can_edit': scoping.can_edit(request.user),
               'currencies': sorted(set(Cost.objects.filter(customer=customer).values_list('currency', flat=True).distinct()) | {currency})}
    if tab == 'accounts':
        context.update(account_tree(customer, month, today, currency))
    elif tab == 'projects':
        context['reconciliation'] = allocation.reconcile(customer, month, min(month + relativedelta(months=1) - timedelta(days=1), today), currency)
        context['projects'] = customer.projects.prefetch_related('rules')
    elif tab == 'budgets':
        budget_list = list(customer.budgets.filter(active=True).select_related('source', 'project').prefetch_related('amounts'))
        evaluations = budgeting.latest_evaluations(budget_list, month)
        context['budget_rows'] = [{'budget': b, 'evaluation': evaluations.get(b.pk), 'amount': b.amount_for(month)} for b in budget_list]
        context['allocation_check'] = budgeting.child_allocation_check(customer, month)
        context['imported_budgets'] = ImportedBudget.objects.filter(source__customer=customer).select_related('source')
        context['alerts'] = Alert.objects.filter(budget__customer=customer, acknowledged_at__isnull=True).select_related('budget')
    elif tab == 'reports':
        from .models import SavedReport
        context['saved_reports'] = [r for r in SavedReport.objects.all() if r.parameters.get('customer') == str(customer.pk)]
        context['monthly'] = list(Cost.objects.filter(customer=customer, currency=currency).annotate(period=TruncMonth('day')).values('period').annotate(u=Sum('unblended'), a=Sum('amortized')).order_by('-period')[:13])
    elif tab == 'sync':
        context['periods'] = CollectionPeriod.objects.filter(source__customer=customer).select_related('source').order_by('source__account_id', '-month')[:60]
        context['jobs'] = Job.objects.filter(source__customer=customer).select_related('source')[:40]
        context['runs'] = customer.syncs.select_related('source')[:20]
    return render(request, 'billing/customer_detail.html', context)


def account_tree(customer, month, today, currency):
    """Payer → member tree with the management account's own spend shown exactly once."""
    month_end = min(month + relativedelta(months=1) - timedelta(days=1), today)
    spend = {r['account_id']: r for r in Cost.objects.filter(customer=customer, currency=currency, day__gte=month, day__lte=month_end).values('account_id').annotate(v=Sum('unblended'), a=Sum('amortized'), services=Count('service', distinct=True))}
    assignments = AccountAssignment.objects.filter(customer=customer).select_related('account', 'account__source').order_by('account__account_id', '-start')
    accounts = {}
    for a in assignments:
        entry = accounts.setdefault(a.account.account_id, {'account': a.account, 'current': False, 'history': []})
        entry['history'].append(a)
        if a.end is None:
            entry['current'] = True
    sources = {s.pk: s for s in customer.sources.all()}
    for s in BillingSource.objects.filter(pk__in=[e['account'].source_id for e in accounts.values() if e['account'].source_id]).select_related('customer'):
        sources.setdefault(s.pk, s)
    tree = []
    used = set()
    for source in sorted(sources.values(), key=lambda s: s.account_id):
        if not source.collects_costs:
            continue
        members = []
        own = None
        for account_id, entry in sorted(accounts.items()):
            account = entry['account']
            if account.source_id != source.pk and account.payer_account_id != source.account_id:
                continue
            used.add(account_id)
            row = {**entry, 'spend': spend.get(account_id)}
            if account_id == source.account_id:
                own = row
            else:
                members.append(row)
        members.sort(key=lambda r: ({'production': 0, 'development': 1}.get(r['account'].environment, 2), -(r['spend']['v'] if r['spend'] else 0)))
        total = sum((r['spend']['v'] for r in members + ([own] if own else []) if r['spend']), Decimal(0))
        tree.append({'source': source, 'own': own, 'members': members, 'count': len(members) + (1 if own else 0), 'total': total if (own and own['spend']) or any(r['spend'] for r in members) else None,
                     'periods': CollectionPeriod.objects.filter(source=source).order_by('-month')[:3]})
    orphans = [{**entry, 'spend': spend.get(account_id)} for account_id, entry in sorted(accounts.items()) if account_id not in used]
    return {'tree': tree, 'orphans': orphans, 'month_end': month_end, 'month_total': sum((r['v'] for r in spend.values()), Decimal(0)) if spend else None}


@require_POST
@staff_required
def customer_offboard(request, pk):
    customer = get_object_or_404(Customer, pk=pk)
    if customer.active:
        try:
            onboarding.offboard_customer(customer, actor=request.user.username)
        except ValueError as exc:
            return HttpResponseBadRequest(str(exc))
        messages.success(request, 'Customer offboarded. Collection stopped; historical records are retained.')
    else:
        onboarding.reactivate_customer(customer, actor=request.user.username)
        messages.success(request, 'Customer reactivated. Resume its connections to collect again.')
    return redirect('customer_detail', pk=pk)


@staff_required
def customer_edit(request, pk):
    customer = get_object_or_404(Customer, pk=pk)
    form = CustomerForm(request.POST or None, instance=customer)
    form.fields.pop('budget')
    if request.method == 'POST' and form.is_valid():
        form.save()
        audit(request, 'Customer updated', customer=customer)
        messages.success(request, 'Customer details updated.')
        return redirect('customer_detail', pk=pk)
    return render(request, 'billing/customer_add.html', {'form': form, 'active_page': 'customers', 'customer': customer, 'editing': True})


# --- connections (wizard steps 2–6) ----------------------------------------------------------------

@staff_required
def source_add(request, pk):
    customer = get_object_or_404(Customer, pk=pk)
    form = SourceForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        source = form.save(commit=False)
        source.customer = customer
        source.onboarding_step = 3
        source.save()
        audit(request, 'Connection added', customer=customer, source=source)
        return redirect('source_detail', pk=source.pk)
    conflicts = onboarding.detect_conflicts(request.POST.get('account_id', ''), customer) if request.method == 'POST' else []
    return render(request, 'billing/source_add.html', {'form': form, 'customer': customer, 'active_page': 'customers', 'steps': onboarding.WIZARD_STEPS, 'step': 2, 'conflicts': conflicts})


@never_cache
@staff_required
def source_detail(request, pk):
    source = get_object_or_404(BillingSource.objects.select_related('customer'), pk=pk)
    form = ConnectionForm(instance=source)
    if request.method == 'POST' and request.POST.get('action') == 'connection':
        form = ConnectionForm(request.POST, instance=source)
        if form.is_valid():
            changed = form.cleaned_data['role_arn'] != BillingSource.objects.get(pk=pk).role_arn
            source = form.save(commit=False)
            if changed:
                source.connection_version += 1
                source.verified_at = None
            source.onboarding_step = max(source.onboarding_step, 4)
            source.last_error = ''
            source.save()
            from .iam import request_allowlist
            request_allowlist(source, request.user.username)
            scheduler.request_verification(source, actor=request.user.username)
            audit(request, 'Role ARN saved; verification queued', customer=source.customer, source=source)
            messages.success(request, 'Role ARN saved. Verification runs in the background within a minute; reload to see the result.')
            return redirect('source_detail', pk=pk)
    accounts = AwsAccount.objects.filter(Q(source=source) | Q(payer_account_id=source.account_id)).prefetch_related('assignments__customer').order_by('account_id')
    pending = Job.objects.filter(source=source, status__in=[Job.QUEUED, Job.LEASED]).order_by('run_after')
    step = source.onboarding_step
    return render(request, 'billing/source_detail.html', {
        'source': source, 'customer': source.customer, 'form': form, 'active_page': 'customers', 'steps': onboarding.WIZARD_STEPS, 'step': step,
        'accounts': accounts, 'pending': pending, 'periods': source.periods.all()[:8], 'runs': source.syncs.all()[:10],
        'imported_budgets': source.imported_budgets.all()[:20], 'assignment_form': AssignmentForm(initial={'customer': source.customer}),
        'jobs': Job.objects.filter(source=source)[:10]})


@staff_required
def source_template(request, pk):
    source = get_object_or_404(BillingSource, pk=pk)
    try:
        body = onboarding.source_template(source)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    response = HttpResponse(body, content_type='application/json')
    response['Content-Disposition'] = f'attachment; filename="customer-billing-role-{source.account_id}.json"'
    return response


@staff_required
def source_setup(request, pk):
    source = get_object_or_404(BillingSource.objects.select_related('customer'), pk=pk)
    import json
    from .iam import policy_bundle
    try:
        bundle = policy_bundle(source)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    return render(request, 'billing/setup_link.html', {
        'source': source, 'customer': source.customer, 'active_page': 'customers',
        'trust_json': json.dumps(bundle['trust_policy'], indent=2),
        'minimum_json': json.dumps(bundle['minimum_permission_policy'], indent=2),
        'optional_json': {k: json.dumps(v, indent=2) for k, v in bundle['optional_permission_policies'].items()},
    })


@require_POST
@staff_required
def source_action(request, pk, action):
    source = get_object_or_404(BillingSource.objects.select_related('customer'), pk=pk)
    actor = request.user.username
    if action == 'verify':
        if not source.role_arn:
            messages.error(request, 'Save the role ARN first.')
        else:
            scheduler.request_verification(source, actor=actor)
            messages.success(request, 'Verification queued; the worker assumes the role and checks capabilities within a minute.')
    elif action == 'discover':
        scheduler.request_discovery(source, actor=actor)
        messages.success(request, 'Account discovery queued.')
    elif action == 'import':
        job, created = scheduler.request_initial_import(source, actor=actor)
        BillingSource.objects.filter(pk=pk).update(onboarding_step=6)
        messages.success(request, 'Initial import queued (six completed months plus the current month).' if created else 'An import is already queued for this connection.')
    elif action == 'refresh':
        scheduler.request_refresh(source, actor=actor)
        messages.success(request, 'Refresh queued.')
    elif action == 'rotate':
        onboarding.rotate_external_id(source, actor=actor)
        messages.success(request, 'New external ID issued. Share the new trust JSON so the customer updates their role, then verify again.')
    elif action == 'pause':
        onboarding.set_paused(source, True, actor=actor)
        messages.success(request, 'Collection paused for this connection.')
    elif action == 'resume':
        onboarding.set_paused(source, False, actor=actor)
        messages.success(request, 'Collection resumed.')
    elif action == 'toggle_shared':
        BillingSource.objects.filter(pk=pk).update(shared=not source.shared)
        audit(request, 'Shared payer flag changed', customer=source.customer, source=source, shared=not source.shared)
        messages.success(request, 'Shared payer setting updated. New accounts now ' + ('enter the review queue.' if not source.shared else 'auto-assign to the owner.'))
    elif action == 'request_budgets':
        capabilities = dict(source.capabilities)
        capabilities['request_budgets'] = not capabilities.get('request_budgets', True)
        BillingSource.objects.filter(pk=pk).update(capabilities=capabilities)
        messages.success(request, 'Template budget-import option updated.')
    else:
        return HttpResponseBadRequest('Unknown action')
    if action not in ('toggle_shared', 'request_budgets'):
        audit(request, f'Connection action: {action}', customer=source.customer, source=source)
    return redirect('source_detail', pk=pk)


# --- accounts -------------------------------------------------------------------------------------

@never_cache
@login_required
def account_detail(request, account_id):
    account = AwsAccount.objects.filter(account_id=account_id).select_related('source', 'source__customer').first()
    try:
        scope = scoping.resolve({'customer': request.GET.get('customer', ''), 'account': account_id})
    except ValueError as exc:
        return render(request, 'billing/filter_error.html', {'error': str(exc), 'active_page': 'customers'}, status=400)
    today = timezone.now().date()
    month = month_param(request)
    currency = request.GET.get('currency', 'USD')
    granularity = 'monthly' if request.GET.get('granularity') == 'monthly' else 'daily'
    costs = scope.costs(Cost.objects.filter(currency=currency))
    if account is None and not costs.exists():
        return render(request, 'billing/filter_error.html', {'error': 'This account is not in the inventory.', 'active_page': 'customers'}, status=404)
    month_end = min(month + relativedelta(months=1) - timedelta(days=1), today)
    in_month = costs.filter(day__gte=month, day__lte=month_end)
    services = list(in_month.values('service').annotate(u=Sum('unblended'), a=Sum('amortized')).order_by('-u'))
    if granularity == 'monthly':
        series = list(costs.annotate(period=TruncMonth('day')).values('period').annotate(u=Sum('unblended'), a=Sum('amortized')).order_by('-period')[:13])
    else:
        series = list(in_month.values('day').annotate(u=Sum('unblended'), a=Sum('amortized')).order_by('day'))
    assignments = AccountAssignment.objects.filter(account__account_id=account_id).select_related('customer').order_by('-start')
    budgets = Budget.objects.filter(scope=Budget.ACCOUNT, account_id=account_id, active=True)
    return render(request, 'billing/account_detail.html', {
        'account': account, 'account_id': account_id, 'scope': scope, 'today': today, 'month': month, 'month_end': month_end, 'currency': currency,
        'granularity': granularity, 'services': services, 'series': series, 'assignments': assignments, 'budgets': budgets,
        'total': in_month.aggregate(v=Sum('unblended'))['v'], 'estimated': in_month.filter(estimated=True).exists(),
        'form': AssignmentForm(initial={'customer': scope.customer.pk if scope.customer else None, 'environment': account.environment if account else ''}),
        'active_page': 'customers', 'can_edit': scoping.can_edit(request.user), 'customer': scope.customer,
        'currencies': sorted(set(Cost.objects.filter(account_id=account_id).values_list('currency', flat=True).distinct()) | {currency})})


@require_POST
@staff_required
def account_assign(request, account_id):
    account = get_object_or_404(AwsAccount, account_id=account_id)
    form = AssignmentForm(request.POST)
    if not form.is_valid():
        messages.error(request, 'Choose a customer and a valid start date.')
        return redirect('account_detail', account_id=account_id)
    customer = form.cleaned_data['customer']
    try:
        ensure_assignment(account, customer, start=form.cleaned_data['start'], note=form.cleaned_data['note'], actor=request.user.username)
    except (ValueError, ValidationError) as exc:
        messages.error(request, str(exc) if isinstance(exc, ValueError) else '; '.join(exc.messages))
        return redirect('account_detail', account_id=account_id)
    if form.cleaned_data.get('environment') is not None:
        AwsAccount.objects.filter(pk=account.pk).update(environment=form.cleaned_data['environment'])
    restamp(account, form.cleaned_data['start'])
    audit(request, 'Account assigned', customer=customer, details={'account_id': account_id, 'start': form.cleaned_data['start'].isoformat()})
    messages.success(request, f'Account {account_id} assigned to {customer.name} from {form.cleaned_data["start"]}. Earlier spend keeps its previous owner.')
    nxt = request.POST.get('next', '')
    if nxt.startswith('/') and not nxt.startswith('//'):
        return redirect(nxt)
    return redirect('account_detail', account_id=account_id)


def restamp(account, start):
    """Re-stamp stored facts from ``start`` onward with the assignment active on each day."""
    assignments = list(account.assignments.all())
    for row in Cost.objects.filter(account_id=account.account_id, day__gte=start).only('id', 'day', 'customer_id'):
        match = next((a for a in assignments if a.covers(row.day)), None)
        owner = match.customer_id if match else None
        if owner != row.customer_id:
            Cost.objects.filter(pk=row.pk).update(customer_id=owner)


@never_cache
@login_required
def unassigned_accounts(request):
    accounts = scoping.unassigned_accounts()
    totals = {r['account_id']: r['v'] for r in Cost.objects.filter(customer__isnull=True, currency='USD').values('account_id').annotate(v=Sum('unblended'))}
    rows = [{'account': a, 'unassigned_spend': totals.get(a.account_id)} for a in accounts]
    return render(request, 'billing/unassigned.html', {'rows': paginate(request, rows), 'form': AssignmentForm(), 'active_page': 'onboarding', 'can_edit': scoping.can_edit(request.user)})


# --- projects -------------------------------------------------------------------------------------

@staff_required
def project_add(request, pk):
    customer = get_object_or_404(Customer, pk=pk)
    form = ProjectForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        project = form.save(commit=False)
        project.customer = customer
        try:
            project.full_clean()
            project.save()
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            audit(request, 'Project created', customer=customer, details={'project': project.name})
            return redirect('project_detail', pk=project.pk)
    return render(request, 'billing/project_form.html', {'form': form, 'customer': customer, 'active_page': 'customers'})


@never_cache
@login_required
def project_detail(request, pk):
    project = get_object_or_404(Project.objects.select_related('customer'), pk=pk)
    today = timezone.now().date()
    month = month_param(request)
    currency = request.GET.get('currency', project.customer.currency or 'USD')
    month_end = min(month + relativedelta(months=1) - timedelta(days=1), today)
    daily = allocation.project_daily(project, month, month_end, currency, 'unblended')
    accounts = allocation.project_accounts(project, month, month_end, currency, 'unblended')
    reconciliation = allocation.reconcile(project.customer, month, month_end, currency)
    budgets = list(project.budgets.filter(active=True).prefetch_related('amounts'))
    evaluations = budgeting.latest_evaluations(budgets, month)
    return render(request, 'billing/project_detail.html', {
        'project': project, 'customer': project.customer, 'month': month, 'month_end': month_end, 'currency': currency, 'today': today,
        'rules': project.rules.all(), 'daily': sorted(daily.items()), 'accounts': accounts,
        'total': sum(daily.values()) if daily else None, 'reconciliation': reconciliation,
        'budget_rows': [{'budget': b, 'evaluation': evaluations.get(b.pk), 'amount': b.amount_for(month)} for b in budgets],
        'active_page': 'customers', 'can_edit': scoping.can_edit(request.user)})


@staff_required
def rule_add(request, pk):
    project = get_object_or_404(Project.objects.select_related('customer'), pk=pk)
    form = AllocationRuleForm(request.POST or None)
    preview = None
    if request.method == 'POST' and form.is_valid():
        rule = form.save(commit=False)
        rule.project = project
        rule.created_by = request.user.username
        rule.version = (project.rules.aggregate(v=Count('pk'))['v'] or 0) + 1
        try:
            allocation.validate_rule(rule, project.customer)
            today = timezone.now().date()
            if request.POST.get('action') == 'preview':
                preview = allocation.preview_rule(rule, project.customer, today.replace(day=1) - relativedelta(months=1), today)
            else:
                rule.save()
                allocation.allocate_project(project)
                audit(request, 'Allocation rule added', customer=project.customer, details={'project': project.name, 'rule': rule.describe()})
                messages.success(request, 'Rule saved. Account rules allocate immediately; tag/category rules complete when the scoped AWS query returns.')
                return redirect('project_detail', pk=project.pk)
        except ValueError as exc:
            form.add_error(None, str(exc))
    return render(request, 'billing/rule_form.html', {'form': form, 'project': project, 'customer': project.customer, 'preview': preview, 'active_page': 'customers',
                                                      'accounts': scoping.customer_accounts(project.customer)})


@require_POST
@staff_required
def rule_retire(request, pk, rule_id):
    project = get_object_or_404(Project, pk=pk)
    rule = get_object_or_404(project.rules, pk=rule_id)
    end = timezone.now().date().replace(day=1)
    if rule.effective_start >= end:
        rule.active = False
    else:
        rule.effective_end = end
    rule.save()
    allocation.allocate_project(project)
    audit(request, 'Allocation rule retired', customer=project.customer, details={'project': project.name, 'rule': rule.pk})
    messages.success(request, 'Rule retired from this month; earlier allocations are preserved.')
    return redirect('project_detail', pk=pk)


# --- budgets ------------------------------------------------------------------------------------------

@never_cache
@login_required
def budget_list(request):
    month = month_param(request)
    query = request.GET.get('q', '').strip()
    status_filter = request.GET.get('status', '')
    scope_filter = request.GET.get('scope', '')
    items = Budget.objects.filter(active=True).select_related('customer', 'source', 'project').prefetch_related('amounts')
    if query:
        items = items.filter(Q(name__icontains=query) | Q(customer__name__icontains=query) | Q(account_id__icontains=query) | Q(project__name__icontains=query))
    if scope_filter in dict(Budget.SCOPES):
        items = items.filter(scope=scope_filter)
    items = list(items)
    evaluations = budgeting.latest_evaluations(items, month)
    rows = [{'budget': b, 'evaluation': evaluations.get(b.pk), 'amount': b.amount_for(month)} for b in items]
    if status_filter:
        rows = [r for r in rows if (r['evaluation'].status if r['evaluation'] else 'Not evaluated') == status_filter]
    sort, direction = sort_param(request, ['customer', 'name', 'amount', 'actual', 'percent', 'status'], 'customer')
    keyfn = {'customer': lambda r: (r['budget'].customer.name.lower(), r['budget'].name.lower()), 'name': lambda r: r['budget'].name.lower(),
             'amount': lambda r: (r['amount'] is None, r['amount'] or 0), 'actual': lambda r: (not r['evaluation'] or r['evaluation'].actual is None, r['evaluation'].actual if r['evaluation'] and r['evaluation'].actual is not None else 0),
             'percent': lambda r: (not r['evaluation'] or r['evaluation'].percent is None, r['evaluation'].percent if r['evaluation'] and r['evaluation'].percent is not None else 0),
             'status': lambda r: r['evaluation'].status if r['evaluation'] else 'zz'}[sort]
    rows.sort(key=keyfn, reverse=direction == 'desc')
    summary = budgeting.portfolio_summary(month, evaluations)
    return render(request, 'billing/budgets.html', {
        'page': paginate(request, rows), 'month': month, 'q': query, 'status_filter': status_filter, 'scope_filter': scope_filter, 'sort': sort, 'dir': direction,
        'summary': summary, 'statuses': budgeting.STATUS_ORDER + ['Not evaluated'], 'scopes': Budget.SCOPES, 'active_page': 'budgets',
        'imported_count': ImportedBudget.objects.count(), 'alerts': Alert.objects.filter(acknowledged_at__isnull=True).select_related('budget', 'budget__customer')[:50],
        'can_edit': scoping.can_edit(request.user), 'customers': Customer.objects.filter(active=True),
        'query_string': '&'.join(f'{k}={v}' for k, v in request.GET.items() if k not in ('page', 'sort', 'dir') and v)})


@staff_required
def budget_add(request):
    customer = get_object_or_404(Customer, pk=request.GET.get('customer') or request.POST.get('customer_id'))
    form = BudgetForm(request.POST or None, customer=customer, initial={'currency': customer.currency, 'scope': request.GET.get('scope', Budget.CUSTOMER),
                                                                         'project': request.GET.get('project'), 'account_id': request.GET.get('account', ''), 'source': request.GET.get('source')})
    if request.method == 'POST' and form.is_valid():
        try:
            budget = form.save(commit=False)
            budget.created_by = request.user.username
            budget.save()
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            budgeting.set_recurring_amount(budget, form.cleaned_data['amount'], form.cleaned_data['effective_from'])
            budgeting.evaluate_budget(budget, timezone.now().date().replace(day=1))
            audit(request, 'Budget created', customer=customer, details={'budget': budget.name, 'scope': budget.scope})
            messages.success(request, 'Budget saved and evaluated.')
            return redirect('budget_detail', pk=budget.pk)
    return render(request, 'billing/budget_form.html', {'form': form, 'customer': customer, 'active_page': 'budgets'})


@never_cache
@login_required
def budget_detail(request, pk):
    budget = get_object_or_404(Budget.objects.select_related('customer', 'source', 'project').prefetch_related('amounts'), pk=pk)
    can_edit = scoping.can_edit(request.user)
    form = BudgetForm(instance=budget, customer=budget.customer)
    override_form = BudgetOverrideForm()
    if request.method == 'POST':
        if not can_edit:
            return HttpResponse('Readers cannot edit budgets.', status=403)
        action = request.POST.get('action')
        if action == 'update':
            form = BudgetForm(request.POST, instance=budget, customer=budget.customer)
            if form.is_valid():
                try:
                    form.save()
                except ValidationError as exc:
                    form.add_error(None, exc)
                else:
                    budgeting.set_recurring_amount(budget, form.cleaned_data['amount'], form.cleaned_data['effective_from'])
                    budgeting.evaluate_budget(budget, timezone.now().date().replace(day=1))
                    audit(request, 'Budget updated', customer=budget.customer, details={'budget': budget.name})
                    messages.success(request, 'Budget updated; the new limit applies from its effective month.')
                    return redirect('budget_detail', pk=pk)
        elif action == 'override':
            override_form = BudgetOverrideForm(request.POST)
            if override_form.is_valid():
                budgeting.set_override(budget, override_form.cleaned_data['month'], override_form.cleaned_data['amount'])
                budgeting.evaluate_budget(budget, override_form.cleaned_data['month'].replace(day=1))
                audit(request, 'Budget month override set', customer=budget.customer, details={'budget': budget.name})
                messages.success(request, 'Specific-month override saved.')
                return redirect('budget_detail', pk=pk)
        elif action == 'deactivate':
            Budget.objects.filter(pk=pk).update(active=False)
            audit(request, 'Budget deactivated', customer=budget.customer, details={'budget': budget.name})
            messages.success(request, 'Budget deactivated; its history is retained.')
            return redirect('budget_list')
        elif action == 'evaluate':
            budgeting.evaluate_budget(budget, month_param(request))
            messages.success(request, 'Budget re-evaluated from stored data.')
            return redirect('budget_detail', pk=pk)
    current = budget.amount_for(timezone.now().date().replace(day=1))
    form.fields['amount'].initial = current
    return render(request, 'billing/budget_detail.html', {
        'budget': budget, 'form': form, 'override_form': override_form, 'evaluations': budget.evaluations.all()[:13], 'amounts': budget.amounts.all(),
        'alerts': budget.alerts.all()[:20], 'active_page': 'budgets', 'can_edit': can_edit, 'current_amount': current})


@require_POST
@staff_required
def alert_ack(request, pk):
    alert = get_object_or_404(Alert.objects.select_related('budget'), pk=pk)
    Alert.objects.filter(pk=pk).update(acknowledged_by=request.user.username, acknowledged_at=timezone.now())
    audit(request, 'Budget alert acknowledged', customer=alert.budget.customer, details={'alert': pk})
    return redirect(request.POST.get('next') if (request.POST.get('next', '').startswith('/') and not request.POST.get('next', '').startswith('//')) else 'budget_list')


@never_cache
@login_required
def imported_budgets(request):
    items = ImportedBudget.objects.select_related('source', 'source__customer')
    query = request.GET.get('q', '').strip()
    if query:
        items = items.filter(Q(name__icontains=query) | Q(owning_account_id__icontains=query) | Q(source__customer__name__icontains=query))
    return render(request, 'billing/imported_budgets.html', {'page': paginate(request, items), 'q': query, 'active_page': 'budgets'})


@staff_required
def budget_bulk(request):
    return bulk_view(request, kind='budgets', parser=budgeting.parse_budget_csv, applier=lambda rows: budgeting.apply_budget_rows(rows, request.user.username),
                     template='billing/bulk_import.html', title='Bulk budgets', columns=budgeting.BUDGET_COLUMNS, done_url='budget_list')


@staff_required
def onboarding_bulk(request):
    return bulk_view(request, kind='customers', parser=onboarding.parse_customer_csv, applier=lambda rows: onboarding.apply_customer_rows(rows, request.user.username),
                     template='billing/bulk_import.html', title='Bulk customer onboarding', columns=onboarding.CUSTOMER_COLUMNS, done_url='onboarding')


def bulk_view(request, kind, parser, applier, template, title, columns, done_url):
    form = CsvUploadForm()
    preview = None
    if request.method == 'POST' and request.POST.get('action') == 'apply':
        preview = get_object_or_404(BulkImport.objects.select_for_update(), pk=request.POST.get('import_id'), kind=kind, uploaded_by=request.user.username)
        if preview.applied_at:
            messages.success(request, 'This preview was already applied; no records were duplicated.')
            return redirect(done_url)
        if preview.errors:
            messages.error(request, 'Fix the validation problems and upload the file again.')
        else:
            try:
                result = applier(preview.rows)
            except ValueError as exc:
                messages.error(request, str(exc))
            else:
                BulkImport.objects.filter(pk=preview.pk).update(applied_at=timezone.now())
                audit(request, f'Bulk {kind} import applied', details={'rows': len(preview.rows)})
                messages.success(request, f'Applied {len(preview.rows)} row(s). {result if isinstance(result, tuple) else ""}'.strip())
                return redirect(done_url)
    elif request.method == 'POST':
        form = CsvUploadForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                text = form.cleaned_data['file'].read().decode('utf-8-sig')
            except UnicodeDecodeError:
                form.add_error('file', 'The file must be UTF-8 encoded CSV.')
            else:
                rows, errors = parser(text)
                preview = BulkImport.objects.create(kind=kind, uploaded_by=request.user.username, rows=rows, errors=errors)
    return render(request, template, {'form': form, 'preview': preview, 'title': title, 'columns': columns, 'kind': kind, 'active_page': 'onboarding' if kind == 'customers' else 'budgets'})


@login_required
def csv_template(request, kind):
    body = onboarding.customer_csv_template() if kind == 'customers' else onboarding.budget_csv_template()
    response = HttpResponse(body, content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="{kind}-template.csv"'
    return response


# --- onboarding operations ---------------------------------------------------------------------------

@never_cache
@login_required
def onboarding_view(request):
    query = request.GET.get('q', '').strip()
    state_filter = request.GET.get('state', '')
    sources = BillingSource.objects.select_related('customer').prefetch_related('periods')
    if query:
        sources = sources.filter(Q(customer__name__icontains=query) | Q(account_id__icontains=query) | Q(customer__reference__icontains=query))
    account_counts = {r['source_id']: r['n'] for r in AwsAccount.objects.filter(source__in=sources).values('source_id').annotate(n=Count('pk'))}
    pending_counts = {r['source_id']: r['n'] for r in Job.objects.filter(source__in=sources, status__in=[Job.QUEUED, Job.LEASED]).values('source_id').annotate(n=Count('pk'))}
    rows = []
    for source in sources:
        state = source.state
        if state_filter and state != state_filter:
            continue
        rows.append({'source': source, 'state': state, 'accounts': account_counts.get(source.pk, 0), 'step': source.onboarding_step,
                     'pending': pending_counts.get(source.pk, 0)})
    sort, direction = sort_param(request, ['customer', 'state', 'step', 'last_success'], 'customer')
    keyfn = {'customer': lambda r: (r['source'].customer.name.lower(), r['source'].account_id), 'state': lambda r: r['state'], 'step': lambda r: r['step'],
             'last_success': lambda r: (r['source'].last_success is None, r['source'].last_success or timezone.now())}[sort]
    rows.sort(key=keyfn, reverse=direction == 'desc')
    counts = {}
    for row in rows:
        counts[row['state']] = counts.get(row['state'], 0) + 1
    return render(request, 'billing/onboarding.html', {'page': paginate(request, rows), 'q': query, 'state_filter': state_filter, 'states': STATES, 'counts': counts,
                                                       'sort': sort, 'dir': direction, 'active_page': 'onboarding', 'can_edit': scoping.can_edit(request.user),
                                                       'unassigned_count': scoping.unassigned_accounts().count(), 'steps': onboarding.WIZARD_STEPS,
                                                       'query_string': '&'.join(f'{k}={v}' for k, v in request.GET.items() if k not in ('page', 'sort', 'dir') and v)})
