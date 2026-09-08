"""Move legacy per-customer connection fields into BillingSource, inventory and budgets.

Customer primary keys, historical costs, saved reports and connection external IDs are
preserved. Every legacy customer becomes exactly one non-shared payer/standalone source;
all of its accounts are assigned to it from the earliest imported day.
"""
from datetime import date
from django.db import migrations
from django.db.models import Min


def forwards(apps, schema_editor):
    Customer = apps.get_model('billing', 'Customer')
    BillingSource = apps.get_model('billing', 'BillingSource')
    AwsAccount = apps.get_model('billing', 'AwsAccount')
    AccountAssignment = apps.get_model('billing', 'AccountAssignment')
    Cost = apps.get_model('billing', 'Cost')
    SyncRun = apps.get_model('billing', 'SyncRun')
    ExplorerQuery = apps.get_model('billing', 'ExplorerQuery')
    Budget = apps.get_model('billing', 'Budget')
    BudgetAmount = apps.get_model('billing', 'BudgetAmount')
    CollectionPeriod = apps.get_model('billing', 'CollectionPeriod')
    for customer in Customer.objects.all():
        source = BillingSource.objects.create(
            customer=customer, kind='payer', account_id=customer.account_id, role_arn=customer.role_arn,
            external_id=str(customer.external_id), enabled=customer.enabled, sync_requested=customer.sync_requested,
            verified_at=customer.verified_at, last_attempt=customer.last_attempt, last_success=customer.last_success,
            last_error=customer.last_error, initial_import_done=customer.last_success is not None,
            discovered_at=customer.last_success, discovery_mode='billing_only' if customer.last_success else '',
            onboarding_step=6 if customer.verified_at else 3 if customer.role_arn else 2)
        Cost.objects.filter(customer=customer).update(source=source)
        SyncRun.objects.filter(customer=customer).update(source=source)
        ExplorerQuery.objects.filter(customer=customer).update(source=source)
        earliest = Cost.objects.filter(customer=customer).aggregate(d=Min('day'))['d'] or date(2000, 1, 1)
        seen = set(Cost.objects.filter(customer=customer).values_list('account_id', flat=True).distinct()) | {customer.account_id}
        for account_id in sorted(seen):
            account, _ = AwsAccount.objects.get_or_create(account_id=account_id, defaults={
                'source': source, 'payer_account_id': customer.account_id, 'discovery': 'billing', 'state': 'UNKNOWN'})
            AccountAssignment.objects.create(account=account, customer=customer, start=min(earliest, date(2000, 1, 1)),
                                             note='Migrated from legacy customer connection', created_by='migration')
        for month in Cost.objects.filter(customer=customer).dates('day', 'month'):
            rows = Cost.objects.filter(customer=customer, day__year=month.year, day__month=month.month)
            CollectionPeriod.objects.create(source=source, month=month, status='complete', revision=1, rows=rows.count(),
                                            first_day=rows.aggregate(d=Min('day'))['d'], last_day=rows.order_by('-day').values_list('day', flat=True).first(),
                                            estimated=rows.filter(estimated=True).exists(), last_success=customer.last_success)
        if customer.budget:
            budget = Budget.objects.create(customer=customer, scope='customer', name=f'{customer.name} monthly budget',
                                           currency=customer.currency, metric='unblended', created_by='migration')
            BudgetAmount.objects.create(budget=budget, amount=customer.budget, effective_from=date(2000, 1, 1))
    # Cached AWS responses whose connection cannot be attributed are disposable.
    ExplorerQuery.objects.filter(source__isnull=True).delete()


def backwards(apps, schema_editor):
    Customer = apps.get_model('billing', 'Customer')
    BillingSource = apps.get_model('billing', 'BillingSource')
    Budget = apps.get_model('billing', 'Budget')
    BudgetAmount = apps.get_model('billing', 'BudgetAmount')
    for source in BillingSource.objects.filter(kind__in=['payer', 'standalone']).order_by('created_at'):
        customer = Customer.objects.get(pk=source.customer_id)
        if Customer.objects.filter(account_id=source.account_id).exclude(pk=customer.pk).exists():
            continue
        customer.account_id = source.account_id
        customer.role_arn = source.role_arn
        customer.enabled = source.enabled
        customer.last_success = source.last_success
        customer.last_error = source.last_error
        customer.verified_at = source.verified_at
        try:
            import uuid
            customer.external_id = uuid.UUID(str(source.external_id))
        except (ValueError, AttributeError):
            pass  # rotated hex IDs cannot be represented in the legacy UUID column
        budget = Budget.objects.filter(customer_id=customer.pk, scope='customer', active=True).first()
        if budget:
            amount = BudgetAmount.objects.filter(budget_id=budget.pk, month__isnull=True).order_by('-effective_from').first()
            customer.budget = amount.amount if amount else None
        customer.save()


class Migration(migrations.Migration):
    dependencies = [('billing', '0003_dashboard_expansion')]
    operations = [migrations.RunPython(forwards, backwards)]
