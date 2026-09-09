"""Create explicitly synthetic UI/recovery fixtures in an empty local test database."""
import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings')
import django
django.setup()
import json,secrets
from pathlib import Path
from datetime import timedelta
from decimal import Decimal
from django.conf import settings
from django.contrib.auth.models import User
from django.utils import timezone
from django_otp.plugins.otp_totp.models import TOTPDevice
from billing.models import *
if not settings.DEBUG or Customer.objects.exists():raise SystemExit('Requires an empty isolated development database.')
password=secrets.token_urlsafe(24)
user=User.objects.create_superuser('synthetic-reviewer',email='reviewer@example.invalid',password=password)
UserSecurity.objects.create(user=user,portfolio_access=True)
device=TOTPDevice.objects.create(user=user,name='synthetic-review',confirmed=True)
customer=Customer.objects.create(name='Synthetic review customer',reference='DEVELOPMENT-ONLY',owner='Synthetic owner')
CustomerMembership.objects.create(user=user,customer=customer,role='operator')
source=BillingSource.objects.create(customer=customer,account_id='000000000001',role_arn='arn:aws:iam::000000000001:role/ExampleFinanceReader',enabled=False)
accounts=[]
for index in range(3):
 account=AwsAccount.objects.create(source=source,account_id=str(index+1).zfill(12),name=f'Synthetic account {index+1}',payer_account_id=source.account_id,discovery='manual',state='ACTIVE')
 AccountAssignment.objects.create(customer=customer,account=account);accounts.append(account)
today=timezone.now().date();first=(today.replace(day=1)-timedelta(days=1)).replace(day=1)
for account in accounts:
 for d in range((today-first).days+1):
  for service,amount in [('Amazon EC2','12.34'),('Amazon S3','2.10')]:
   Cost.objects.create(source=source,customer=customer,account_id=account.account_id,day=first+timedelta(days=d),service=service,currency='USD',unblended=Decimal(amount),amortized=Decimal(amount),estimated=(first+timedelta(days=d)).month==today.month)
for month in [first,today.replace(day=1)]:CollectionPeriod.objects.create(source=source,month=month,status='complete',last_success=timezone.now(),first_day=month,last_day=min(today,(month+timedelta(days=32)).replace(day=1)-timedelta(days=1)),estimated=month.month==today.month)
record=AllianceRecord.objects.create(customer=customer,account=accounts[0],month=first,currency='USD',account_name='Synthetic production account',notes='Development-only handoff note',summary_note='Synthetic recovery fixture',snapshot={'current':'447.64','state':'Complete'},revision=1)
AllianceServiceNote.objects.create(record=record,service='Amazon EC2',commentary='Development-only service note')
SavedReport.objects.create(customer=customer,name='Synthetic monthly report',created_by=user.username,parameters={'customer':str(customer.pk)})
CustomerApproval.objects.create(customer=customer,contacts=['Development-only contact'],expected_accounts=[x.account_id for x in accounts],billing_fields=['unblended','amortized'],status='pending')
p=Path('.deployment/review.json');p.parent.mkdir(exist_ok=True);p.write_text(json.dumps({'username':user.username,'password':password,'customer':str(customer.pk),'source':str(source.pk),'device_id':device.pk}));p.chmod(0o600)
print('Synthetic review fixtures created. Private login details saved only under ignored .deployment/.')
