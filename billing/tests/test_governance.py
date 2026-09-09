import csv
import io
from datetime import date,timedelta
from decimal import Decimal
from unittest.mock import patch
from django.test import TestCase,override_settings
from django.contrib.auth.models import User
from django.utils import timezone
from django.core.exceptions import PermissionDenied
from billing.models import *
from billing.tests.helpers import make_customer,cost,assign
from billing.reconciliation import reconcile
from billing.onboarding import parse_customer_csv,apply_customer_rows,offboard_customer
from billing.governance import readiness
from billing.monitoring import scan,deliver
from billing.portal import invite,accept
from billing.alliance import customer_rollups
from billing.access import context,for_user

class GovernanceTests(TestCase):
    def setUp(self):
        self.customer,self.source=make_customer('Synthetic Customer','123456789012',accounts=['210987654321'])
        self.day=date.today().replace(day=1)
        cost(self.source,self.day,'100');cost(self.source,self.day,'-10',service='Credit')
        CollectionPeriod.objects.create(source=self.source,month=self.day,status='complete',last_success=timezone.now(),estimated=False,first_day=self.day,last_day=date.today())
    def reference(self,rows,**scope):
        out=io.StringIO();w=csv.writer(out);w.writerow(['account_id','amount','currency','metric','start','end','timezone','estimated'])
        for account,amount in rows:
            w.writerow([account,amount,scope.get('currency','USD'),'unblended',str(self.day),str(self.day),'UTC','false'])
        return out.getvalue()
    def test_reconciliation_includes_credits_and_evidenced_zero(self):
        result=reconcile(self.customer,self.reference([(self.source.account_id,'90'),('210987654321','0')]),self.day,self.day,'unblended','USD')
        self.assertTrue(result['passed']);self.assertEqual(result['aws_end_exclusive'],str(self.day+timedelta(days=1)))
    def test_reconciliation_rejects_currency_foreign_scope_and_missing(self):
        with self.assertRaisesRegex(ValueError,'currency'):reconcile(self.customer,self.reference([(self.source.account_id,'90')],currency='EUR'),self.day,self.day,'unblended','USD')
        with self.assertRaisesRegex(ValueError,'outside'):reconcile(self.customer,self.reference([('999999999999','90')]),self.day,self.day,'unblended','USD')
        CollectionPeriod.objects.all().delete()
        result=reconcile(self.customer,self.reference([(self.source.account_id,'90'),('210987654321','0')]),self.day,self.day,'unblended','USD')
        self.assertFalse(result['passed']);self.assertIn('missing data',[r['state'] for r in result['accounts']])
    def test_bulk_multiple_sources_explicit_identity_and_idempotent(self):
        text='name,reference,account_id,kind\nSynthetic New,SYN-1,333333333333,payer\nSynthetic New,SYN-1,444444444444,standalone\n'
        rows,errors=parse_customer_csv(text);self.assertEqual(errors,[])
        apply_customer_rows(rows,'test');apply_customer_rows(rows,'test')
        new=Customer.objects.get(reference='SYN-1');self.assertEqual(new.sources.count(),2)
        self.assertFalse(new.sources.filter(verified_at__isnull=False).exists())
    def test_similar_name_is_not_merged(self):
        rows,errors=parse_customer_csv('name,account_id\nSynthetic Customer,333333333333\n')
        self.assertTrue(errors)
        with self.assertRaises(ValueError):apply_customer_rows(rows,'test')
    def test_offboarding_cancels_leases_access_and_preserves_costs(self):
        user=User.objects.create_user('member');CustomerMembership.objects.create(user=user,customer=self.customer,role='viewer')
        job=Job.objects.create(source=self.source,key='leased',kind='collect',status=Job.LEASED,worker='test')
        offboard_customer(self.customer,'test')
        self.source.refresh_from_db();job.refresh_from_db()
        self.assertFalse(self.source.enabled);self.assertEqual(job.status,Job.FAILED)
        self.assertFalse(CustomerMembership.objects.get(user=user).active)
        self.assertEqual(Cost.objects.count(),2)
        from billing.jobs import complete
        complete(job);job.refresh_from_db();self.assertEqual(job.status,Job.FAILED)
        self.assertGreater(OffboardingRecord.objects.get().sessions_expire_after,timezone.now())
    def test_readiness_never_invents_real_approvals(self):
        self.assertFalse(readiness(self.customer)['ready'])
        self.assertIn('iam_approval',readiness(self.customer)['pending'])
    @override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
    def test_monitor_dedup_acknowledgement_escalation_test_destination(self):
        now=timezone.now()
        for i in range(3):Job.objects.create(source=self.source,key=f'failed-{i}',kind='collect',status=Job.FAILED,finished_at=now)
        scan(now);scan(now)
        event=OperationalAlert.objects.get(kind='collection_failure')
        AlertRoute.objects.create(customer=self.customer,recipients=['test@example.invalid'],enabled=True,escalation_minutes=60)
        self.assertEqual(deliver(),0)
        self.assertEqual(deliver(test=True,now=now),1)
        self.assertEqual(deliver(test=True,now=now),0)
        self.assertEqual(deliver(test=True,now=now+timedelta(hours=2)),1)
        event.acknowledged_at=now;event.save()
        self.assertEqual(deliver(test=True,now=now+timedelta(hours=3)),0)
        from django.core import mail
        self.assertEqual(len(mail.outbox),2)
    def test_portal_default_gate(self):
        with self.assertRaises(PermissionDenied):invite(self.customer,'test@example.invalid',[],'test')
    @override_settings(EXTERNAL_PORTAL_ENABLED=True)
    def test_portal_single_use_exact_identity_and_isolation(self):
        user=User.objects.create_user('external',email='test@example.invalid')
        with patch('billing.portal.readiness',return_value={'ready':True}):
            token=invite(self.customer,user.email,[self.source.account_id],'test')
            accept(user,token)
            with self.assertRaises(PermissionDenied):accept(user,token)
        other,source=make_customer('Other synthetic','999999999999');cost(source,self.day,999)
        with context(for_user(user)):
            self.assertEqual(Customer.objects.count(),1)
            self.assertEqual(Cost.objects.count(),2)
            self.assertEqual(AwsAccount.objects.count(),1)

    def test_retention_gates_and_customer_scoped_purge(self):
        from billing.retention import purge,plan
        other,source=make_customer('Retained neighbor','999999999999');cost(source,self.day,999)
        offboard_customer(self.customer,'test')
        record=OffboardingRecord.objects.get(customer=self.customer)
        with self.assertRaises(ValueError):purge(record,'test','synthetic disposition')
        past=timezone.now()-timedelta(days=1)
        record.retention_until=past;record.sessions_expire_after=past;record.allowlist_removed_at=past
        record.trust_revocation_reference='synthetic trust evidence';record.deletion_approved_reference='synthetic retention approval';record.save()
        result=purge(record,'test','synthetic backup and exports expired')
        self.assertEqual(result['database_rows']['costs'],2)
        self.assertEqual(Cost.objects.filter(customer=self.customer).count(),0)
        self.assertEqual(Cost.objects.filter(customer=other).count(),1)
        self.assertTrue(AccountAssignment.objects.filter(customer=self.customer).exists())
        self.assertTrue(AuditEvent.objects.filter(action='Retention deletion completed').exists())

    def test_minimum_severity_and_multiple_recipients(self):
        from billing.monitoring import observe
        from django.core import mail
        event,_=observe(self.customer,'source_delay','synthetic-route','Synthetic source delay','info')
        AlertRoute.objects.create(customer=self.customer,recipients=['one@example.invalid'],severity='warning',enabled=True)
        self.assertEqual(deliver(test=True),0)
        AlertRoute.objects.create(customer=self.customer,recipients=['two@example.invalid'],severity='info',enabled=True)
        AlertRoute.objects.create(customer=self.customer,recipients=['three@example.invalid'],severity='info',enabled=True)
        self.assertEqual(deliver(test=True),1)
        self.assertEqual(set(mail.outbox[-1].to),{'two@example.invalid','three@example.invalid'})
