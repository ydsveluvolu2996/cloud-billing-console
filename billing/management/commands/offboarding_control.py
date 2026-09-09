import json
from django.conf import settings
from django.core.management.base import BaseCommand,CommandError
from django.utils import timezone
from billing.models import OffboardingRecord
from billing.retention import plan,purge
from billing.authentication import security_event


class Command(BaseCommand):
    help='Admin-only offboarding evidence and retention plan/purge. Does not change AWS or external backups.'
    def add_arguments(self,p):
        p.add_argument('action',choices=['plan','record-allowlist-removal','record-trust-revocation','approve-deletion','purge'])
        p.add_argument('--record',required=True,type=int);p.add_argument('--actor',required=True)
        p.add_argument('--evidence',default='');p.add_argument('--external-evidence',default='')
    def handle(self,*args,**o):
        if settings.RUNTIME_ROLE!='admin':raise CommandError('Use the isolated administration database identity.')
        record=OffboardingRecord.objects.get(pk=o['record']);action=o['action']
        if action=='plan':self.stdout.write(json.dumps(plan(record),indent=2));return
        if not o['evidence'].strip():raise CommandError('An evidence reference is required.')
        if action=='purge':
            try:result=purge(record,o['actor'],o['external_evidence'])
            except ValueError as exc:raise CommandError(str(exc)) from None
            self.stdout.write(json.dumps(result,indent=2));return
        field={'record-allowlist-removal':'allowlist_removed_at','record-trust-revocation':'trust_revocation_reference','approve-deletion':'deletion_approved_reference'}[action]
        setattr(record,field,timezone.now() if action=='record-allowlist-removal' else o['evidence'])
        record.save(update_fields=[field]);security_event(o['actor'],'Offboarding '+action,customer=record.customer,target=str(record.pk),evidence=o['evidence'])
        self.stdout.write('Evidence recorded; no AWS or backup change was performed.')
