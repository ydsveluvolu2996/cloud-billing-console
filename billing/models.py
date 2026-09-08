"""Customer, connection, inventory, allocation, budget and job models.

Business customers are separated from AWS credential connections (``BillingSource``).
Cost facts are collected once per source and stamped with the customer that owned
the linked account on that day, based on effective-dated ``AccountAssignment`` rows.
"""
import secrets
import uuid
from datetime import date, timedelta
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, RegexValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

ACCOUNT_ID = RegexValidator(r'^\d{12}$', 'Enter a 12-digit AWS account ID.')
CURRENCY = RegexValidator(r'^[A-Z]{3}$', 'Use a three-letter currency code.')
METRIC_CHOICES = [('unblended', 'Unblended'), ('amortized', 'Amortized')]


def new_external_id():
    """High-entropy per-connection external ID (hex, 40 characters)."""
    return secrets.token_hex(20)


class Customer(models.Model):
    """A business customer. Primary keys are preserved from earlier releases."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=120, unique=True)
    reference = models.CharField('Internal reference', max_length=60, blank=True)
    owner = models.CharField('Account owner', max_length=120, blank=True)
    currency = models.CharField(max_length=3, default='USD', validators=[CURRENCY])
    active = models.BooleanField(default=True)
    offboarded_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

    # --- aggregate connection state -------------------------------------------------
    @property
    def cost_sources(self):
        return [s for s in self.sources.all() if s.kind != BillingSource.MEMBER_BUDGETS]

    @property
    def status(self):
        if not self.active:
            return 'Offboarded'
        sources = self.cost_sources
        if not sources:
            return 'Awaiting setup'
        states = [s.state for s in sources]
        for state in ('Permission problem', 'Partial data', 'Stale data', 'Initial import running',
                      'Awaiting customer setup', 'Connection verified', 'Account discovery complete'):
            if state in states:
                return state
        if all(state == 'Paused' for state in states):
            return 'Paused'
        return 'Connected'

    @property
    def last_success(self):
        times = [s.last_success for s in self.cost_sources if s.last_success]
        return max(times) if times else None

    @property
    def sync_requested(self):
        return any(s.sync_requested for s in self.cost_sources)

    @property
    def enabled(self):
        return self.active and any(s.enabled for s in self.cost_sources)


class BillingSource(models.Model):
    """One IAM role connection. A payer source covers its whole organization."""
    PAYER, STANDALONE, MEMBER_BUDGETS = 'payer', 'standalone', 'member_budgets'
    KINDS = [(PAYER, 'Management / payer account'), (STANDALONE, 'Standalone account'),
             (MEMBER_BUDGETS, 'Member budget reader (no cost collection)')]
    ROLE_PATH = 'role/BillingConsole/CostReadOnly'
    STALE_AFTER = timedelta(hours=12)

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name='sources')
    kind = models.CharField(max_length=20, choices=KINDS, default=PAYER)
    account_id = models.CharField(max_length=12, validators=[ACCOUNT_ID])
    role_arn = models.CharField(max_length=300, blank=True)
    external_id = models.CharField(max_length=64, default=new_external_id, editable=False)
    shared = models.BooleanField(default=False, help_text='Payer serves several customers; accounts need explicit assignment.')
    enabled = models.BooleanField(default=True)
    connection_version = models.PositiveIntegerField(default=1)
    capabilities = models.JSONField(default=dict, blank=True)
    discovery_mode = models.CharField(max_length=20, blank=True)  # organizations | billing_only
    onboarding_step = models.PositiveSmallIntegerField(default=2)
    sync_requested = models.BooleanField(default=False)
    initial_import_done = models.BooleanField(default=False)
    verified_at = models.DateTimeField(null=True, blank=True)
    discovered_at = models.DateTimeField(null=True, blank=True)
    last_attempt = models.DateTimeField(null=True, blank=True)
    last_success = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=500, blank=True)
    next_run = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['customer__name', 'account_id']
        constraints = [models.UniqueConstraint(fields=['account_id'], condition=Q(kind__in=['payer', 'standalone']),
                                               name='unique_cost_source_account')]

    def __str__(self):
        return f'{self.customer.name} · {self.account_id}'

    @property
    def expected_role_arn(self):
        return f'arn:aws:iam::{self.account_id}:{self.ROLE_PATH}'

    def clean(self):
        super().clean()
        if self.role_arn and self.role_arn != self.expected_role_arn:
            raise ValidationError({'role_arn': 'Use the CostReadOnly role created by this customer’s onboarding template.'})

    @property
    def collects_costs(self):
        return self.kind in (self.PAYER, self.STANDALONE)

    @property
    def state(self):
        if not self.enabled:
            return 'Paused'
        if not self.role_arn or not self.verified_at:
            return 'Awaiting customer setup'
        if self.last_error and ('AccessDenied' in self.last_error or 'permission' in self.last_error.lower()):
            return 'Permission problem'
        if not self.discovered_at:
            return 'Connection verified'
        if not self.last_success and not self.last_attempt:
            return 'Account discovery complete'
        if not self.initial_import_done:
            return 'Initial import running'
        if self.last_error or self.periods.filter(status__in=['failed', 'partial']).exists():
            return 'Partial data'
        if self.last_success and self.last_success < timezone.now() - self.STALE_AFTER:
            return 'Stale data'
        return 'Connected'


class AwsAccount(models.Model):
    """Inventory of every AWS account seen through any connection."""
    account_id = models.CharField(max_length=12, unique=True, validators=[ACCOUNT_ID])
    name = models.CharField(max_length=200, blank=True)
    email = models.CharField(max_length=254, blank=True)
    state = models.CharField(max_length=30, blank=True)  # ACTIVE, SUSPENDED, PENDING_CLOSURE, CLOSED, UNKNOWN
    payer_account_id = models.CharField(max_length=12, blank=True)
    source = models.ForeignKey(BillingSource, null=True, blank=True, on_delete=models.SET_NULL, related_name='accounts')
    environment = models.CharField(max_length=40, blank=True)  # production, development, other
    joined_at = models.DateTimeField(null=True, blank=True)
    first_seen = models.DateTimeField(default=timezone.now)
    last_seen = models.DateTimeField(default=timezone.now)
    missing_since = models.DateTimeField(null=True, blank=True)
    discovery = models.CharField(max_length=20, blank=True)  # organizations | billing | manual

    class Meta:
        ordering = ['account_id']

    def __str__(self):
        return f'{self.account_id} {self.name}'.strip()

    @property
    def is_management(self):
        return bool(self.payer_account_id) and self.payer_account_id == self.account_id

    def owner_on(self, day):
        return next((a for a in self.assignments.all() if a.covers(day)), None)


class AccountAssignment(models.Model):
    """Effective-dated ownership of an account by a customer."""
    account = models.ForeignKey(AwsAccount, on_delete=models.PROTECT, related_name='assignments')
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name='assignments')
    start = models.DateField(default=date(2000, 1, 1))
    end = models.DateField(null=True, blank=True, help_text='Exclusive; blank means current.')
    note = models.CharField(max_length=200, blank=True)
    created_by = models.CharField(max_length=150, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['account__account_id', 'start']
        constraints = [models.CheckConstraint(condition=Q(end__isnull=True) | Q(end__gt=models.F('start')), name='assignment_end_after_start')]

    def covers(self, day):
        return self.start <= day and (self.end is None or day < self.end)

    def clean(self):
        super().clean()
        others = AccountAssignment.objects.filter(account_id=self.account_id).exclude(pk=self.pk)
        for other in others:
            overlap = other.start < (self.end or date.max) and self.start < (other.end or date.max)
            if overlap:
                raise ValidationError('Assignments for one account cannot overlap. End the previous assignment first.')


class Cost(models.Model):
    """Daily cost facts grouped by linked account and service; additive."""
    source = models.ForeignKey(BillingSource, on_delete=models.PROTECT, related_name='costs', null=True)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name='costs', null=True, blank=True)
    day = models.DateField()
    account_id = models.CharField(max_length=12)
    service = models.CharField(max_length=200)
    currency = models.CharField(max_length=10)
    unblended = models.DecimalField(max_digits=24, decimal_places=10)
    amortized = models.DecimalField(max_digits=24, decimal_places=10)
    estimated = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['source', 'day', 'account_id', 'service', 'currency'], name='unique_cost_slice')]
        indexes = [models.Index(fields=['day', 'currency']), models.Index(fields=['customer', 'day']),
                   models.Index(fields=['account_id', 'day']), models.Index(fields=['customer', 'currency', 'day'], name='billing_cost_scope_day_idx')]


class CollectionPeriod(models.Model):
    """Per-source, per-month coverage and publication metadata."""
    source = models.ForeignKey(BillingSource, on_delete=models.CASCADE, related_name='periods')
    month = models.DateField()
    status = models.CharField(max_length=20, default='pending')  # pending, complete, failed, partial
    revision = models.PositiveIntegerField(default=0)
    attempts = models.PositiveIntegerField(default=0)
    rows = models.PositiveIntegerField(default=0)
    request_count = models.PositiveIntegerField(default=0)
    page_count = models.PositiveIntegerField(default=0)
    duration_ms = models.PositiveIntegerField(default=0)
    first_day = models.DateField(null=True, blank=True)
    last_day = models.DateField(null=True, blank=True)
    estimated = models.BooleanField(default=True)
    last_attempt = models.DateTimeField(null=True, blank=True)
    last_success = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ['-month']
        constraints = [models.UniqueConstraint(fields=['source', 'month'], name='unique_collection_period')]


class SyncRun(models.Model):
    source = models.ForeignKey(BillingSource, on_delete=models.PROTECT, related_name='syncs', null=True)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name='syncs', null=True)
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True)
    status = models.CharField(max_length=20, default='running')
    rows = models.PositiveIntegerField(default=0)
    requests = models.PositiveIntegerField(default=0)
    error = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ['-started_at']


class Project(models.Model):
    """Logical cost allocation spanning a customer's accounts."""
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name='projects')
    name = models.CharField(max_length=120)
    code = models.CharField(max_length=40, blank=True)
    description = models.CharField(max_length=300, blank=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['customer__name', 'name']
        constraints = [models.UniqueConstraint(fields=['customer', 'name'], name='unique_project_name')]

    def __str__(self):
        return self.name

    def rule_on(self, day):
        return next((r for r in self.rules.all() if r.active and r.covers(day)), None)


class AllocationRule(models.Model):
    """Versioned, effective-dated disjoint allocation rule for one project."""
    ACCOUNTS, TAG, COST_CATEGORY, ACCOUNT_TAG, ACCOUNT_CATEGORY = 'accounts', 'tag', 'cost_category', 'account_tag', 'account_category'
    KINDS = [(ACCOUNTS, 'Linked accounts'), (TAG, 'Cost allocation tag'), (COST_CATEGORY, 'AWS cost category'),
             (ACCOUNT_TAG, 'Accounts + tag'), (ACCOUNT_CATEGORY, 'Accounts + cost category')]
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='rules')
    version = models.PositiveIntegerField(default=1)
    priority = models.PositiveIntegerField(default=100)
    kind = models.CharField(max_length=20, choices=KINDS)
    account_ids = models.JSONField(default=list, blank=True)
    key = models.CharField(max_length=120, blank=True)  # tag key or cost category name
    values = models.JSONField(default=list, blank=True)
    effective_start = models.DateField(default=date(2000, 1, 1))
    effective_end = models.DateField(null=True, blank=True)
    active = models.BooleanField(default=True)
    created_by = models.CharField(max_length=150, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['priority', 'pk']

    def covers(self, day):
        return self.effective_start <= day and (self.effective_end is None or day < self.effective_end)

    @property
    def uses_accounts(self):
        return self.kind in (self.ACCOUNTS, self.ACCOUNT_TAG, self.ACCOUNT_CATEGORY)

    @property
    def uses_aws_query(self):
        return self.kind != self.ACCOUNTS

    def describe(self):
        parts = []
        if self.uses_accounts:
            parts.append('accounts ' + ', '.join(self.account_ids))
        if self.kind in (self.TAG, self.ACCOUNT_TAG):
            parts.append(f'tag {self.key} in {", ".join(self.values)}')
        if self.kind in (self.COST_CATEGORY, self.ACCOUNT_CATEGORY):
            parts.append(f'category {self.key} in {", ".join(self.values)}')
        return ' and '.join(parts)


class ProjectCost(models.Model):
    """Allocated daily cost per project; derived from facts or scoped AWS queries."""
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='costs')
    rule = models.ForeignKey(AllocationRule, on_delete=models.CASCADE, related_name='costs')
    source = models.ForeignKey(BillingSource, on_delete=models.CASCADE, related_name='project_costs')
    day = models.DateField()
    account_id = models.CharField(max_length=12)
    currency = models.CharField(max_length=10)
    unblended = models.DecimalField(max_digits=24, decimal_places=10)
    amortized = models.DecimalField(max_digits=24, decimal_places=10)
    estimated = models.BooleanField(default=True)
    computed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['rule', 'source', 'day', 'account_id', 'currency'], name='unique_project_cost')]
        indexes = [models.Index(fields=['project', 'day'])]


class Budget(models.Model):
    """Dashboard-defined budget at customer, payer, account or project scope."""
    CUSTOMER, SOURCE, ACCOUNT, PROJECT = 'customer', 'source', 'account', 'project'
    SCOPES = [(CUSTOMER, 'Customer'), (SOURCE, 'Payer connection'), (ACCOUNT, 'Linked account'), (PROJECT, 'Project')]
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name='budgets')
    scope = models.CharField(max_length=10, choices=SCOPES, default=CUSTOMER)
    source = models.ForeignKey(BillingSource, null=True, blank=True, on_delete=models.PROTECT, related_name='budgets')
    account_id = models.CharField(max_length=12, blank=True)
    project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.PROTECT, related_name='budgets')
    name = models.CharField(max_length=120)
    currency = models.CharField(max_length=3, default='USD', validators=[CURRENCY])
    metric = models.CharField(max_length=10, choices=METRIC_CHOICES, default='unblended')
    filters = models.JSONField(default=dict, blank=True)  # {"include_services": [...], "exclude_services": [...]}
    actual_threshold = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('80'))
    forecast_threshold = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('100'))
    active = models.BooleanField(default=True)
    created_by = models.CharField(max_length=150, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['customer__name', 'scope', 'name']

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        if self.scope == self.SOURCE and not self.source_id:
            raise ValidationError({'source': 'Choose the payer connection for this budget.'})
        if self.scope == self.ACCOUNT and not self.account_id:
            raise ValidationError({'account_id': 'Enter the linked account for this budget.'})
        if self.scope == self.PROJECT and not self.project_id:
            raise ValidationError({'project': 'Choose the project for this budget.'})
        if self.project_id and self.project.customer_id != self.customer_id:
            raise ValidationError({'project': 'The project belongs to another customer.'})
        if self.source_id and self.source.customer_id != self.customer_id and not self.source.shared:
            raise ValidationError({'source': 'The connection belongs to another customer.'})

    def amount_for(self, month):
        """Specific-month override first, then the latest recurring amount effective on/before the month."""
        amounts = list(self.amounts.all())
        override = next((a for a in amounts if a.month == month), None)
        if override:
            return override.amount
        recurring = [a for a in amounts if a.month is None and a.effective_from <= month and (a.effective_to is None or month < a.effective_to)]
        if not recurring:
            return None
        return max(recurring, key=lambda a: a.effective_from).amount

    @property
    def scope_label(self):
        if self.scope == self.SOURCE and self.source_id:
            return f'Payer {self.source.account_id}'
        if self.scope == self.ACCOUNT:
            return f'Account {self.account_id}'
        if self.scope == self.PROJECT and self.project_id:
            return f'Project {self.project.name}'
        return 'Customer'


class BudgetAmount(models.Model):
    """Recurring monthly limits (effective-dated) or specific-month overrides."""
    budget = models.ForeignKey(Budget, on_delete=models.CASCADE, related_name='amounts')
    amount = models.DecimalField(max_digits=18, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    effective_from = models.DateField(default=date(2000, 1, 1))
    effective_to = models.DateField(null=True, blank=True)
    month = models.DateField(null=True, blank=True, help_text='Set for a specific-month override.')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-month', '-effective_from']


class BudgetEvaluation(models.Model):
    budget = models.ForeignKey(Budget, on_delete=models.CASCADE, related_name='evaluations')
    month = models.DateField()
    evaluated_at = models.DateTimeField(default=timezone.now)
    amount = models.DecimalField(max_digits=18, decimal_places=2, null=True)
    actual = models.DecimalField(max_digits=24, decimal_places=10, null=True)
    forecast = models.DecimalField(max_digits=24, decimal_places=10, null=True)
    forecast_method = models.CharField(max_length=30, blank=True)  # aws, run_rate, unavailable
    status = models.CharField(max_length=30, default='Not configured')
    data_status = models.CharField(max_length=30, default='missing')  # complete, partial, stale, missing
    detail = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['budget', 'month'], name='unique_budget_evaluation')]
        ordering = ['-month']

    @property
    def remaining(self):
        return None if self.amount is None or self.actual is None else self.amount - self.actual

    @property
    def percent(self):
        return None if not self.amount or self.actual is None else float(self.actual / self.amount * 100)

    @property
    def forecast_variance(self):
        return None if self.amount is None or self.forecast is None else self.forecast - self.amount


class ImportedBudget(models.Model):
    """Read-only snapshot of an AWS Budget owned by the connected account."""
    source = models.ForeignKey(BillingSource, on_delete=models.CASCADE, related_name='imported_budgets')
    owning_account_id = models.CharField(max_length=12)
    name = models.CharField(max_length=200)
    arn = models.CharField(max_length=400, blank=True)
    budget_type = models.CharField(max_length=40)
    time_unit = models.CharField(max_length=20)
    limit_amount = models.DecimalField(max_digits=24, decimal_places=10, null=True)
    limit_unit = models.CharField(max_length=20, blank=True)
    time_period = models.JSONField(default=dict, blank=True)
    filters = models.JSONField(default=dict, blank=True)
    actual_amount = models.DecimalField(max_digits=24, decimal_places=10, null=True)
    actual_unit = models.CharField(max_length=20, blank=True)
    forecast_amount = models.DecimalField(max_digits=24, decimal_places=10, null=True)
    forecast_unit = models.CharField(max_length=20, blank=True)
    calculated_at = models.DateTimeField(null=True, blank=True)
    aws_updated_at = models.DateTimeField(null=True, blank=True)
    raw = models.JSONField(default=dict, blank=True)
    imported_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['owning_account_id', 'name']
        constraints = [models.UniqueConstraint(fields=['source', 'owning_account_id', 'name'], name='unique_imported_budget')]

    @property
    def is_cost_budget(self):
        return self.budget_type == 'COST'

    @property
    def is_percentage(self):
        return (self.limit_unit or '').upper() in ('PERCENTAGE', 'PERCENT')


class Alert(models.Model):
    budget = models.ForeignKey(Budget, on_delete=models.CASCADE, related_name='alerts')
    month = models.DateField()
    kind = models.CharField(max_length=10)  # actual | forecast
    threshold = models.DecimalField(max_digits=6, decimal_places=2)
    value = models.DecimalField(max_digits=24, decimal_places=10, null=True)
    message = models.CharField(max_length=300)
    triggered_at = models.DateTimeField(default=timezone.now)
    acknowledged_by = models.CharField(max_length=150, blank=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-triggered_at']
        constraints = [models.UniqueConstraint(fields=['budget', 'month', 'kind', 'threshold'], name='unique_alert')]


class Job(models.Model):
    """Durable PostgreSQL-backed job with leases, retries and coalescing keys."""
    QUEUED, LEASED, DONE, FAILED = 'queued', 'leased', 'done', 'failed'
    kind = models.CharField(max_length=40)
    key = models.CharField(max_length=200)
    source = models.ForeignKey(BillingSource, null=True, blank=True, on_delete=models.CASCADE, related_name='jobs')
    payload = models.JSONField(default=dict, blank=True)
    progress = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=10, default=QUEUED)
    priority = models.PositiveSmallIntegerField(default=5)
    run_after = models.DateTimeField(default=timezone.now)
    lease_expires = models.DateTimeField(null=True, blank=True)
    worker = models.CharField(max_length=80, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    max_attempts = models.PositiveIntegerField(default=6)
    last_error = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    duration_ms = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['status', 'run_after', 'priority']), models.Index(fields=['key'])]
        constraints = [models.UniqueConstraint(fields=['key'], condition=Q(status__in=['queued', 'leased']), name='unique_active_job_key')]


class AuditEvent(models.Model):
    at = models.DateTimeField(auto_now_add=True)
    actor = models.CharField(max_length=150)
    action = models.CharField(max_length=100)
    customer = models.ForeignKey(Customer, null=True, on_delete=models.PROTECT)
    source = models.ForeignKey(BillingSource, null=True, blank=True, on_delete=models.SET_NULL)
    details = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-at']


class ExplorerQuery(models.Model):
    """A single connection/AWS request; old data survives unsuccessful refreshes."""
    source = models.ForeignKey(BillingSource, on_delete=models.CASCADE, related_name='explorer_queries', null=True)
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name='explorer_queries', null=True, blank=True)
    fingerprint = models.CharField(max_length=64)
    operation = models.CharField(max_length=50)
    parameters = models.JSONField(default=dict)
    connection_fingerprint = models.CharField(max_length=64)
    data = models.JSONField(null=True, blank=True)
    requested = models.BooleanField(default=True)
    last_used = models.DateTimeField(default=timezone.now)
    last_attempt = models.DateTimeField(null=True, blank=True)
    last_success = models.DateTimeField(null=True, blank=True)
    error = models.CharField(max_length=500, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['source', 'fingerprint'], name='unique_explorer_query')]
        indexes = [models.Index(fields=['requested', 'last_used'])]


class SavedReport(models.Model):
    name = models.CharField(max_length=120)
    parameters = models.JSONField(default=dict)
    created_by = models.CharField(max_length=150)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name', 'pk']


class BulkImport(models.Model):
    """CSV preview/apply record for customers or budgets."""
    kind = models.CharField(max_length=20)  # customers | budgets
    uploaded_by = models.CharField(max_length=150)
    created_at = models.DateTimeField(auto_now_add=True)
    rows = models.JSONField(default=list)
    errors = models.JSONField(default=list)
    applied_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    @property
    def valid(self):
        return not self.errors
