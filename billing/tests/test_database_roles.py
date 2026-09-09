"""Actual PostgreSQL login tests; never substitute table-owner access for runtime evidence."""
import secrets
from pathlib import Path
from unittest import skipUnless
from contextlib import contextmanager
from django.test import TransactionTestCase,override_settings,Client
from django.db import connection
from django.contrib.auth.models import User
from django.conf import settings
from django.utils import timezone
from billing.models import CustomerMembership,UserSecurity,Cost,Customer
from billing.tests.helpers import make_customer,cost,TEST_STORAGES
from datetime import date
import psycopg
from psycopg import sql


@skipUnless(connection.vendor=='postgresql','Requires PostgreSQL and role administration in an isolated test database')
class DatabaseRoleTests(TransactionTestCase):
    def setUp(self):
        with connection.cursor() as c:c.execute((settings.BASE_DIR/'deploy/database-roles.sql').read_text())
        self.a,self.sa=make_customer('RLS Alpha','123456789012');self.b,self.sb=make_customer('RLS Beta','210987654321')
        cost(self.sa,date.today(),10);cost(self.sb,date.today(),999)
        self.user=User.objects.create_user('rls-viewer',password='test-only-unique-password')
        CustomerMembership.objects.create(user=self.user,customer=self.a,role='operator')
        self.password=secrets.token_urlsafe(32)
        with connection.cursor() as c:
            for role in ['billing_web','billing_collector']:
                c.execute(sql.SQL('ALTER ROLE {} PASSWORD {}').format(sql.Identifier(role),sql.Literal(self.password)).as_string())
    def connect(self,role):
        cfg=connection.settings_dict
        return psycopg.connect(host=cfg['HOST'],port=cfg['PORT'] or 5432,dbname=cfg['NAME'],user=role,password=self.password)
    def test_web_identity_rls_and_administration_denial(self):
        with self.connect('billing_web') as c:
            self.assertEqual(c.execute('SELECT current_user').fetchone()[0],'billing_web')
            self.assertEqual(c.execute('SELECT count(*) FROM billing_cost').fetchone()[0],0)
            c.execute("SELECT set_config('billing.user_id',%s,true)",[str(self.user.pk)])
            self.assertEqual(c.execute('SELECT DISTINCT customer_id FROM billing_cost').fetchall(),[(self.a.pk,)])
            self.assertEqual(c.execute('SELECT count(*) FROM billing_cost WHERE customer_id=%s',[self.b.pk]).fetchone()[0],0)
            self.assertFalse(c.execute("SELECT rolsuper OR rolbypassrls OR rolcreaterole FROM pg_roles WHERE rolname=current_user").fetchone()[0])
            self.assertEqual(c.execute("SELECT count(*) FROM pg_tables WHERE schemaname='public' AND tableowner=current_user").fetchone()[0],0)
            for statement in ['CREATE TABLE forbidden(id int)','UPDATE billing_usersecurity SET portfolio_access=true',"UPDATE billing_roleapproval SET status='approved'",'DELETE FROM billing_auditevent','DELETE FROM billing_cost','CREATE ROLE forbidden_role']:
                with self.subTest(statement=statement):
                    with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                        with c.transaction():c.execute(statement)
    def test_collector_identity_has_operational_not_identity_permissions(self):
        with self.connect('billing_collector') as c:
            self.assertEqual(c.execute('SELECT count(*) FROM billing_cost').fetchone()[0],2)
            self.assertFalse(c.execute("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user").fetchone()[0])
            for statement in ["UPDATE billing_billingsource SET role_arn='bad'",'UPDATE billing_customermembership SET active=false','SELECT password FROM auth_user','DELETE FROM billing_auditevent','CREATE TABLE forbidden(id int)']:
                with self.subTest(statement=statement):
                    with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                        with c.transaction():c.execute(statement)

    def test_effective_transfer_requires_both_scopes_and_keeps_historical_facts(self):
        from billing.models import AccountAssignment
        from billing.collector import ensure_assignment
        from django.db import IntegrityError, transaction
        assignment=AccountAssignment.objects.get(customer=self.a)
        old=Cost.objects.get(customer=self.a);old.day=date(2026,1,1);old.save()
        recent=cost(self.sa,date.today(),25)
        ensure_assignment(assignment.account,self.b,start=date(2026,2,1),actor='synthetic-admin')
        with self.assertRaises(IntegrityError),transaction.atomic():
            AccountAssignment.objects.create(account=assignment.account,customer=self.a,start=date(2026,3,1))
        with self.connect('billing_web') as c:
            c.execute("SELECT set_config('billing.user_id',%s,true)",[str(self.user.pk)])
            with self.assertRaises(psycopg.errors.RaiseException),c.transaction():
                c.execute('SELECT billing_restamp_ownership(%s,%s)',[self.sa.account_id,date(2026,2,1)])
        CustomerMembership.objects.create(user=self.user,customer=self.b,role='operator')
        with self.connect('billing_web') as c:
            c.execute("SELECT set_config('billing.user_id',%s,true)",[str(self.user.pk)])
            self.assertEqual(c.execute('SELECT billing_restamp_ownership(%s,%s)',[self.sa.account_id,date(2026,2,1)]).fetchone()[0],1)
        old.refresh_from_db();recent.refresh_from_db()
        self.assertEqual(old.customer_id,self.a.pk);self.assertEqual(recent.customer_id,self.b.pk)
        self.assertEqual(recent.unblended,25)
    @override_settings(SECURE_SSL_REDIRECT=False,ALLOWED_HOSTS=['testserver'],STORAGES=TEST_STORAGES,DATABASE_RLS_ENABLED=True)
    def test_django_requests_use_actual_web_login(self):
        cfg=connection.settings_dict;old_user,old_password=cfg['USER'],cfg['PASSWORD']
        connection.close();cfg['USER']='billing_web';cfg['PASSWORD']=self.password
        try:
            client=Client();client.force_login(self.user)
            s=client.session;s['mfa_at']=timezone.now().timestamp();s.save()
            response=client.get('/customers/')
            self.assertEqual(response.status_code,200)
            self.assertContains(response,'RLS Alpha');self.assertNotContains(response,'RLS Beta')
            self.assertEqual(client.get(f'/customers/{self.b.pk}/').status_code,404)
            self.assertEqual(client.get('/mfa/').status_code,200)
            self.assertEqual(client.get(f'/customers/{self.a.pk}/governance/').status_code,200)
        finally:
            connection.close();cfg['USER']=old_user;cfg['PASSWORD']=old_password

    def test_account_restricted_login_and_shared_source_container(self):
        from billing.tests.helpers import assign
        from billing.models import AccountAssignment,BillingSource
        shared=BillingSource.objects.create(customer=self.b,account_id='444444444444',shared=True)
        assign('333333333333',self.a,shared)
        member=CustomerMembership.objects.get(user=self.user);member.account_ids=['333333333333'];member.save()
        with self.connect('billing_web') as c:
            c.execute("SELECT set_config('billing.user_id',%s,true)",[str(self.user.pk)])
            self.assertEqual(c.execute('SELECT id FROM billing_customer').fetchall(),[(self.a.pk,)])
            self.assertEqual(c.execute('SELECT account_id FROM billing_awsaccount').fetchall(),[('333333333333',)])
            self.assertEqual(c.execute('SELECT id FROM billing_billingsource').fetchall(),[(shared.pk,)])
            self.assertEqual(c.execute('SELECT count(*) FROM billing_cost').fetchone()[0],0)
            with self.assertRaises(psycopg.errors.InsufficientPrivilege),c.transaction():
                c.execute("UPDATE billing_customer SET name='forbidden' WHERE id=%s",[self.a.pk])

    @override_settings(SECURE_SSL_REDIRECT=False,ALLOWED_HOSTS=['testserver'],STORAGES=TEST_STORAGES,DATABASE_RLS_ENABLED=True)
    def test_actual_web_login_can_offboard_without_identity_admin_grants(self):
        cfg=connection.settings_dict;old_user,old_password=cfg['USER'],cfg['PASSWORD']
        connection.close();cfg['USER']='billing_web';cfg['PASSWORD']=self.password
        try:
            client=Client();client.force_login(self.user)
            s=client.session;s['mfa_at']=timezone.now().timestamp();s.save()
            response=client.post(f'/customers/{self.a.pk}/offboard/')
            self.assertEqual(response.status_code,302)
        finally:
            connection.close();cfg['USER']=old_user;cfg['PASSWORD']=old_password
        self.assertFalse(CustomerMembership.objects.get(user=self.user).active)
        self.assertFalse(Customer.objects.get(pk=self.a.pk).active)
        self.assertEqual(Cost.objects.filter(customer=self.a).count(),1)

    def test_portal_admission_actual_web_role_exact_token_and_readiness(self):
        import hashlib
        from billing.models import CustomerApproval,RolloutReadiness,ReconciliationRun,RoleApproval,PortalInvitation
        external=User.objects.create_user('rls-external',email='rls@example.invalid')
        UserSecurity.objects.create(user=external)
        token=secrets.token_urlsafe(48);digest=hashlib.sha256(token.encode()).hexdigest()
        PortalInvitation.objects.create(customer=self.a,target_user=external,email=external.email,token_hash=digest,created_by='test',account_ids=[self.sa.account_id],expires_at=timezone.now()+__import__('datetime').timedelta(hours=1))
        CustomerApproval.objects.create(customer=self.a,status='approved',evidence='synthetic-test',contacts=['synthetic'],authorized_users=['synthetic'],billing_fields=['cost'],expected_accounts=[self.sa.account_id],storage_region='ap-south-1',retention_days=30,approved_at=timezone.now(),approved_by='test')
        RolloutReadiness.objects.create(customer=self.a,owner='test',planned_date=date.today(),security_evidence='synthetic-test',independent_review='synthetic-test',checklist={'rollback_readiness':True,'rollback_evidence':'synthetic-test'})
        self.sa.refresh_from_db()
        ReconciliationRun.objects.create(customer=self.a,start=date.today(),end=date.today(),metric='unblended',currency='USD',scope={},result={'passed':True,'configuration':{str(self.sa.pk):[self.sa.connection_version,self.sa.ownership_version]}},reference='synthetic-test-fixture',synthetic=False,created_by='test')
        self.sa.refresh_from_db();self.sa.trust_checks={'correct_external_id':'passed','missing_external_id':'denied','wrong_external_id':'denied','account_identity':'passed','exact_collector_principal':'passed','connection_version':self.sa.connection_version};self.sa.save()
        RoleApproval.objects.create(source=self.sa,role_arn=self.sa.role_arn,connection_version=self.sa.connection_version,status='approved',requested_by='test',approved_at=timezone.now(),approved_by='test',evidence='synthetic-test')
        with self.connect('billing_web') as c:
            c.execute("SELECT set_config('billing.user_id',%s,true)",[str(external.pk)])
            with self.assertRaises(psycopg.errors.RaiseException),c.transaction():c.execute('SELECT billing_accept_invitation(%s)',[digest])
            c.execute("SELECT set_config('billing.external_enabled','true',true)")
            with self.assertRaises(psycopg.errors.RaiseException),c.transaction():c.execute('SELECT billing_accept_invitation(%s)',['wrong-token'])
            self.assertEqual(c.execute('SELECT billing_accept_invitation(%s)',[digest]).fetchone()[0],self.a.pk)
            self.assertEqual(c.execute('SELECT DISTINCT customer_id FROM billing_cost').fetchall(),[(self.a.pk,)])
            with self.assertRaises(psycopg.errors.RaiseException),c.transaction():c.execute('SELECT billing_accept_invitation(%s)',[digest])

    @override_settings(REQUIRE_CONNECTION_APPROVAL=False)
    def test_concurrent_sources_cannot_publish_duplicate_account_days(self):
        import threading
        from decimal import Decimal
        from django.db import connections
        from billing.collector import publish_month,OverlappingBillingScope
        from billing.aws import Meter
        Cost.objects.all().delete()
        barrier=threading.Barrier(2);outcomes=[]
        today=date.today()
        records=[{'day':today,'account_id':self.sa.account_id,'service':'Synthetic service','currency':'USD','unblended':Decimal('10'),'amortized':Decimal('10'),'estimated':True}]
        def publish(source):
            try:
                barrier.wait(timeout=5)
                publish_month(source,today.replace(day=1),records,[today],True,Meter(),timezone.now())
                outcomes.append('published')
            except OverlappingBillingScope:outcomes.append('overlap denied')
            except Exception as exc:outcomes.append(type(exc).__name__)
            finally:connections.close_all()
        threads=[threading.Thread(target=publish,args=(s,)) for s in (self.sa,self.sb)]
        for t in threads:t.start()
        for t in threads:t.join(timeout=10)
        self.assertEqual(sorted(outcomes),['overlap denied','published'])
        self.assertEqual(Cost.objects.count(),1)
