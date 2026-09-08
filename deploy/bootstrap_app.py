"""Run once inside the app container. Reads bootstrap secrets from stdin, never logs them."""
import json
import sys
from django.contrib.auth.models import User
from billing.models import Customer, AuditEvent

data = json.load(sys.stdin)
if not User.objects.filter(username=data['username']).exists():
    User.objects.create_superuser(username=data['username'], password=data['password'])
customer, created = Customer.objects.get_or_create(account_id=data['account_id'], defaults={
    'name': data['customer_name'], 'external_id': data['external_id'],
    'role_arn': f"arn:aws:iam::{data['account_id']}:role/BillingConsole/CostReadOnly", 'sync_requested': True})
if created:
    AuditEvent.objects.create(actor='deployment', action='Internal AWS account connected', customer=customer)
print('Administrator and internal account initialized.')
