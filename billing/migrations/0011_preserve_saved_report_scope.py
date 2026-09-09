from uuid import UUID
from django.db import migrations


def preserve_scope(apps,schema_editor):
    Customer=apps.get_model('billing','Customer');SavedReport=apps.get_model('billing','SavedReport')
    known=set(Customer.objects.values_list('pk',flat=True))
    for report in SavedReport.objects.filter(customer__isnull=True).iterator():
        try:cid=UUID(str((report.parameters or {}).get('customer','')))
        except (ValueError,TypeError,AttributeError):continue
        if cid in known:SavedReport.objects.filter(pk=report.pk).update(customer_id=cid)


class Migration(migrations.Migration):
    dependencies=[('billing','0010_billingsource_ownership_version')]
    operations=[migrations.RunPython(preserve_scope,migrations.RunPython.noop)]
