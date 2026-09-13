from contextlib import contextmanager, nullcontext
from datetime import date
from decimal import Decimal
from django.test import TransactionTestCase
from django.conf import settings
from django.db import connection, transaction
from django.db.migrations.executor import MigrationExecutor

class MigrationPreservationTests(TransactionTestCase):
    @contextmanager
    def schema_policy_lifecycle(self):
        """Deploy-managed policies target the latest schema, not historical states.

        Django 6 no longer silently cascades DROP COLUMN to dependent policies.
        Detach only our managed policies for this isolated historical-schema test,
        retaining RLS (no policy means deny), then reinstall after upgrading. On
        PostgreSQL the whole transition is atomic, including failure rollback.
        """
        postgres = connection.vendor == 'postgresql'
        with transaction.atomic() if postgres else nullcontext():
            if postgres:
                with connection.cursor() as cursor:
                    # Exercise the production-policy case even when run alone.
                    cursor.execute((settings.BASE_DIR / 'deploy/database-roles.sql').read_text())
                    cursor.execute("SELECT tablename, policyname FROM pg_policies WHERE schemaname = 'public' AND policyname IN ('web_scope', 'collector_scope')")
                    policies = set(cursor.fetchall())
                    for table, policy in policies:
                        cursor.execute(f'DROP POLICY {connection.ops.quote_name(policy)} ON {connection.ops.quote_name(table)}')
                    cursor.execute("SELECT count(*) FROM pg_class WHERE relname = 'billing_cost' AND relrowsecurity")
                    self.assertEqual(cursor.fetchone()[0], 1)
            latest = MigrationExecutor(connection).loader.graph.leaf_nodes()
            try:
                yield
            finally:
                if postgres:
                    with connection.cursor() as cursor:
                        cursor.execute('SET CONSTRAINTS ALL IMMEDIATE')
                MigrationExecutor(connection).migrate(latest)
                if postgres:
                    with connection.cursor() as cursor:
                        cursor.execute((settings.BASE_DIR / 'deploy/database-roles.sql').read_text())
                        cursor.execute("SELECT tablename, policyname FROM pg_policies WHERE schemaname = 'public' AND policyname IN ('web_scope', 'collector_scope')")
                        self.assertEqual(set(cursor.fetchall()), policies)
                        cursor.execute("SELECT relrowsecurity FROM pg_class WHERE relname = 'billing_cost'")
                        self.assertTrue(cursor.fetchone()[0])
                        cursor.execute('SET LOCAL ROLE billing_web')
                        cursor.execute('SELECT count(*) FROM billing_cost')
                        self.assertEqual(cursor.fetchone()[0], 0)
                        cursor.execute('RESET ROLE')

    def test_existing_cost_ownership_alliance_and_saved_reports_survive(self):
        with self.schema_policy_lifecycle():
            self.preserve_historical_rows()

    def preserve_historical_rows(self):
        executor=MigrationExecutor(connection)
        latest=executor.loader.graph.leaf_nodes()
        old=('billing','0007_alliancerecord_allianceservicenote_and_more')
        executor.migrate([old])
        apps=executor.loader.project_state([old]).apps
        try:
            Customer=apps.get_model('billing','Customer');Source=apps.get_model('billing','BillingSource')
            Account=apps.get_model('billing','AwsAccount');Assignment=apps.get_model('billing','AccountAssignment')
            Cost=apps.get_model('billing','Cost');Record=apps.get_model('billing','AllianceRecord')
            Note=apps.get_model('billing','AllianceServiceNote');Report=apps.get_model('billing','SavedReport')
            customer=Customer.objects.create(name='Synthetic migration preservation')
            source=Source.objects.create(customer=customer,account_id='123456789012')
            account=Account.objects.create(source=source,account_id=source.account_id)
            Assignment.objects.create(customer=customer,account=account,start=date(2020,1,1),end=date(2025,1,1))
            Cost.objects.create(customer=customer,source=source,account_id=account.account_id,day=date(2024,1,1),service='Synthetic service',currency='USD',unblended=Decimal('-12.345'),amortized=Decimal('-12.345'),estimated=False)
            record=Record.objects.create(customer=customer,account=account,month=date(2024,1,1),currency='USD',legal_entity='Synthetic legal entity',notes='Preserve comments',summary_note='Preserve handoff',snapshot={'current':'-12.345','captured_at':'2024-02-01T00:00:00Z'},revision=3)
            Note.objects.create(record=record,service='Synthetic service',commentary='Preserve service note')
            report=Report.objects.create(name='Historical report',parameters={'customer':str(customer.pk)},created_by='old-individual')
            ext=source.external_id
            if connection.vendor == 'postgresql':
                with connection.cursor() as cursor:
                    cursor.execute('SET CONSTRAINTS ALL IMMEDIATE')
            executor=MigrationExecutor(connection);executor.migrate(latest)
            from billing import models
            self.assertEqual(models.Customer.objects.get(pk=customer.pk).name,customer.name)
            self.assertEqual(models.BillingSource.objects.get(pk=source.pk).external_id,ext)
            self.assertEqual(models.Cost.objects.get(customer_id=customer.pk).unblended,Decimal('-12.345'))
            self.assertEqual(models.AccountAssignment.objects.get(customer_id=customer.pk).end,date(2025,1,1))
            saved=models.AllianceRecord.objects.get(pk=record.pk)
            self.assertEqual(saved.snapshot,record.snapshot);self.assertEqual(saved.notes,'Preserve comments');self.assertEqual(saved.revision,3)
            self.assertEqual(saved.service_notes.get().commentary,'Preserve service note')
            revision=saved.revisions.get(revision=3)
            self.assertEqual(revision.snapshot,record.snapshot)
            self.assertEqual(revision.service_notes['Synthetic service'],'Preserve service note')
            self.assertEqual(models.SavedReport.objects.get(pk=report.pk).customer_id,customer.pk)
            self.assertFalse(models.CustomerApproval.objects.exists())
            self.assertFalse(models.CustomerMembership.objects.exists())
        finally:
            MigrationExecutor(connection).migrate(latest)
