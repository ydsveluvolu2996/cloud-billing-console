"""Run once inside the app container. Reads bootstrap secrets from stdin, never logs them."""
import json
import sys
from django.contrib.auth.models import User
from billing.models import AuditEvent, BillingSource, Customer

data = json.load(sys.stdin)
if not User.objects.filter(username=data['username']).exists():
    User.objects.create_superuser(username=data['username'], password=data['password'])
customer, created = Customer.objects.get_or_create(name=data['customer_name'])
source, source_created = BillingSource.objects.get_or_create(account_id=data['account_id'], kind__in=['payer', 'standalone'], defaults={
    'customer': customer, 'kind': 'payer', 'external_id': data['external_id'],
    'role_arn': f"arn:aws:iam::{data['account_id']}:role/BillingConsole/CostReadOnly", 'onboarding_step': 4})
if source_created:
    from billing import scheduler
    scheduler.request_verification(source, actor='deployment')
    AuditEvent.objects.create(actor='deployment', action='Internal AWS account connected', customer=customer, source=source)
print('Administrator and internal account initialized.')
