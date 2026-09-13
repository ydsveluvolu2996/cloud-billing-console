from datetime import date
from decimal import Decimal
from django.test import TransactionTestCase
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

class MigrationPreservationTests(TransactionTestCase):
    def test_existing_cost_ownership_alliance_and_saved_reports_survive(self):
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
