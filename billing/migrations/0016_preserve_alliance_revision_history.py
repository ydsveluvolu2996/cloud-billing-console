from django.db import migrations


def preserve(apps,schema_editor):
    Record=apps.get_model('billing','AllianceRecord')
    Revision=apps.get_model('billing','AllianceRevision')
    Audit=apps.get_model('billing','AuditEvent')
    db=schema_editor.connection.alias
    for record in Record.objects.using(db).select_related('account').iterator(chunk_size=200):
        events=Audit.objects.using(db).filter(customer_id=record.customer_id,action__startswith='Alliance',
            details__account=record.account.account_id,details__month=str(record.month),details__currency=record.currency).order_by('at')
        for event in events.iterator(chunk_size=200):
            details=event.details
            revision=details.get('revision')
            if not isinstance(revision,int) or revision<1:continue
            Revision.objects.using(db).get_or_create(record_id=record.pk,revision=revision,defaults={
                'actor':event.actor,'created_at':event.at,'snapshot':details.get('snapshot') if isinstance(details.get('snapshot'),dict) else {},
                'fields':details.get('fields') if isinstance(details.get('fields'),dict) else {},
                'service_notes':details.get('service_notes') if isinstance(details.get('service_notes'),dict) else {}})
        if record.revision:
            fields={f.name:str(getattr(record,f.name)) for f in Record._meta.fields if not f.is_relation and f.name not in ('id','snapshot')}
            notes=dict(record.service_notes.using(db).values_list('service','commentary'))
            Revision.objects.using(db).get_or_create(record_id=record.pk,revision=record.revision,defaults={
                'actor':record.updated_by,'created_at':record.updated_at,'snapshot':record.snapshot,'fields':fields,'service_notes':notes})


class Migration(migrations.Migration):
    dependencies=[('billing','0015_alliancerevision')]
    operations=[migrations.RunPython(preserve,migrations.RunPython.noop)]
