"""Reproducible heterogeneous 100-customer workload on an empty isolated database."""
import json
import time
import threading
from datetime import date,timedelta
from decimal import Decimal
from pathlib import Path
from django.conf import settings
from django.core.management.base import CommandError
from django.contrib.auth.models import User
from django.db import connections,close_old_connections
from django.test import Client,override_settings
from django.utils import timezone
from billing.models import Customer,BillingSource,AwsAccount,AccountAssignment,Cost,CollectionPeriod,CustomerMembership,UserSecurity,Job
from billing.collector import ensure_assignment
from .synthetic_load import Command as Base, percentile


class Command(Base):
    help='100 synthetic customers with 2/15/101 base accounts, multiple payers, standalone/shared sources and scoped concurrent readers.'
    def add_arguments(self,p):
        super().add_arguments(p)
        p.add_argument('--recheck',action='store_true',help='Repeat measurements on an existing isolated billing_security_* synthetic database')
        p.set_defaults(customers=100,accounts=3932,months=7,services=3,readers=8,requests=80,collect_sources=130,output='docs/evidence/security-load.json',keep=True)
    def handle(self,*args,**o):
        if not settings.DEBUG or (Customer.objects.exists() and not o['recheck']):raise CommandError('Requires DEBUG and an empty isolated database; existing records are never removed.')
        if o['customers']<100:raise CommandError('This workload requires at least 100 synthetic customers.')
        o['keep']=True
        path=Path(o['output'])
        if o['recheck']:
            if not settings.DATABASES['default']['NAME'].startswith('billing_security_') or Customer.objects.exclude(name__startswith='Synthetic ').exists():raise CommandError('Recheck requires an isolated synthetic database')
            results=json.loads(path.read_text())
            results.update(rechecked_at=timezone.now().isoformat(),collection_cycle=self.simulate_collection(o),dashboard=self.measure_dashboard(o),table_sizes=self.table_sizes())
        else:
            super().handle(*args,**o)
            results=json.loads(path.read_text())
        results['scoped_concurrency']=self.scoped_concurrency(o)
        results['workload']={'base_account_counts':[2,15,101],'standalone_sources':BillingSource.objects.filter(kind='standalone').count(),'shared_sources':BillingSource.objects.filter(shared=True).count(),'total_sources':BillingSource.objects.count(),'customers':Customer.objects.count(),'account_count':AwsAccount.objects.count()}
        results['targets']={'scoped_p95_ms':2000,'collection_cycle_seconds':21600,'cross_customer_disclosures':0,'http_errors':0}
        results['targets_met']={'scoped_p95':results['scoped_concurrency']['p95_ms']<=2000,'isolation':not results['scoped_concurrency']['failures'],'collection_cycle':results['collection_cycle']['wall_seconds']<21600,'http_errors':not results['dashboard']['failures']}
        results['hardware_note']='Record CPU, RAM, OS and database version in the adjacent performance report.'
        path.write_text(json.dumps(results,indent=2)+'\n')
        if not all(results['targets_met'].values()):raise CommandError('Synthetic acceptance target failed; inspect evidence')
        self.stdout.write(json.dumps({'workload':results['workload'],'scoped_concurrency':results['scoped_concurrency'],'targets_met':results['targets_met']},indent=2))
    def generate(self,o):
        result=super().generate(o)
        customers=list(Customer.objects.order_by('name'));today=timezone.now().date()
        first=Cost.objects.order_by('day').values_list('day',flat=True).first()
        for index,customer in enumerate(customers):
            kinds=(['standalone'] if index%5==0 else [])+(['payer'] if index%10==0 else [])
            for j,kind in enumerate(kinds):
                account_id=str(900000000000+index*10+j)
                source=BillingSource.objects.create(customer=customer,account_id=account_id,kind=kind,role_arn=f'arn:aws:iam::{account_id}:role/SyntheticReader',verified_at=timezone.now(),discovered_at=timezone.now(),last_success=timezone.now(),initial_import_done=True)
                self.approve_fixture_source(source, [account_id])
                account=AwsAccount.objects.create(source=source,account_id=account_id,payer_account_id=account_id,discovery='manual')
                AccountAssignment.objects.create(account=account,customer=customer)
                Cost.objects.bulk_create([Cost(source=source,customer=customer,account_id=account_id,day=first+timedelta(days=d),service='Synthetic service',currency='USD',unblended=Decimal('1.23'),amortized=Decimal('1.23'),estimated=(first+timedelta(days=d)).month==today.month) for d in range((today-first).days+1)])
            if index%10==0:
                source=customer.sources.filter(kind='payer').first();source.shared=True;source.save(update_fields=['shared'])
                account=source.accounts.exclude(account_id=source.account_id).first()
                if account:
                    transfer=today.replace(day=1)-timedelta(days=15)
                    ensure_assignment(account,customers[(index+1)%len(customers)],start=transfer,actor='synthetic-load',note='Synthetic shared payer transfer')
                    Cost.objects.filter(account_id=account.account_id,day__gte=transfer).update(customer=customers[(index+1)%len(customers)])
        result['cost_rows']=Cost.objects.count();result['accounts']=AwsAccount.objects.count()
        return result
    def measure_dashboard(self,o):
        user,_=User.objects.get_or_create(username='synthetic-reader',defaults={'is_staff':True,'is_superuser':True})
        UserSecurity.objects.update_or_create(user=user,defaults={'portfolio_access':True})
        with override_settings(MFA_REQUIRED=False):return super().measure_dashboard(o)
    def scoped_concurrency(self,o):
        customers=list(Customer.objects.order_by('name')[:o['readers']]);clients=[]
        for i,customer in enumerate(customers):
            user,_=User.objects.get_or_create(username=f'synthetic-scoped-{i}')
            CustomerMembership.objects.update_or_create(user=user,customer=customer,defaults={'role':'viewer','active':True})
            c=Client();c.force_login(user);session=c.session;session['mfa_at']=timezone.now().timestamp();session.save();clients.append((c,customer))
        latencies=[];failures=[];lock=threading.Lock()
        def read(index):
            close_old_connections();c,customer=clients[index]
            local=[];bad=[]
            for _ in range(max(1,o['requests']//len(clients))):
                t=time.perf_counter()
                try:
                    response=c.get('/customers/')
                    if response.status_code!=200:bad.append({'reader':index,'status':response.status_code})
                    body=response.content.decode()
                    if any(other.name in body for other in customers if other.pk!=customer.pk):bad.append({'reader':index,'error':'cross-customer disclosure'})
                    if customer.name not in body:bad.append({'reader':index,'error':'authorized customer absent'})
                except Exception as exc:bad.append({'reader':index,'error':type(exc).__name__})
                local.append((time.perf_counter()-t)*1000)
            connections.close_all()
            with lock:latencies.extend(local);failures.extend(bad)
        with override_settings(SECURE_SSL_REDIRECT=False,ALLOWED_HOSTS=['testserver']):
            threads=[threading.Thread(target=read,args=(i,)) for i in range(len(clients))]
            begin=time.perf_counter()
            for t in threads:t.start()
            for t in threads:t.join()
        return {'readers':len(clients),'requests':len(latencies),'p50_ms':round(percentile(latencies,50),1),'p95_ms':round(percentile(latencies,95),1),'seconds':round(time.perf_counter()-begin,2),'failures':failures}
