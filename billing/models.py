import uuid
from datetime import timedelta
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator, MinValueValidator
from django.db import models
from django.utils import timezone


class Customer(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=120, unique=True)
    account_id = models.CharField(max_length=12, unique=True, validators=[RegexValidator(r'^\d{12}$', 'Enter a 12-digit AWS account ID.')])
    role_arn = models.CharField(max_length=300, blank=True)
    external_id = models.UUIDField(default=uuid.uuid4, editable=False)
    budget = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(Decimal('0.01'))])
    currency = models.CharField(max_length=3, default='USD', validators=[RegexValidator(r'^[A-Z]{3}$', 'Use a three-letter currency code.')])
    enabled = models.BooleanField(default=True)
    sync_requested = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    last_attempt = models.DateTimeField(null=True, blank=True)
    last_success = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=500, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['name']

    def clean(self):
        super().clean()
        if self.role_arn and self.role_arn != f'arn:aws:iam::{self.account_id}:role/BillingConsole/CostReadOnly':
            raise ValidationError({'role_arn': 'Use the CostReadOnly role created by this customer’s onboarding template.'})

    @property
    def status(self):
        if not self.enabled:
            return 'Paused'
        if not self.role_arn:
            return 'Awaiting setup'
        if self.last_error:
            return 'Action required'
        if not self.last_success:
            return 'Waiting for sync'
        if self.last_success < timezone.now() - timedelta(hours=12):
            return 'Stale'
        return 'Connected'

    def __str__(self):
        return self.name


class Cost(models.Model):
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name='costs')
    day = models.DateField()
    account_id = models.CharField(max_length=12)
    service = models.CharField(max_length=200)
    currency = models.CharField(max_length=10)
    unblended = models.DecimalField(max_digits=24, decimal_places=10)
    amortized = models.DecimalField(max_digits=24, decimal_places=10)
    estimated = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['customer', 'day', 'account_id', 'service', 'currency'], name='unique_cost_slice')]
        indexes = [models.Index(fields=['day', 'currency']), models.Index(fields=['customer', 'day'])]


class SyncRun(models.Model):
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name='syncs')
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True)
    status = models.CharField(max_length=20, default='running')
    rows = models.PositiveIntegerField(default=0)
    error = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ['-started_at']


class AuditEvent(models.Model):
    at = models.DateTimeField(auto_now_add=True)
    actor = models.CharField(max_length=150)
    action = models.CharField(max_length=100)
    customer = models.ForeignKey(Customer, null=True, on_delete=models.PROTECT)

    class Meta:
        ordering = ['-at']
