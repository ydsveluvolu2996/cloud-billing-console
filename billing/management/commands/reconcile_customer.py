import json
from datetime import date
from pathlib import Path
from django.core.management.base import BaseCommand,CommandError
from billing.models import Customer,BillingSource,ReconciliationRun
from billing.reconciliation import reconcile
from billing.authentication import security_event

class Command(BaseCommand):
    help='Reconcile an authorized customer reference CSV against historical ownership. Never calls AWS.'
    def add_arguments(self,p):
        for name in ('customer','reference-csv','reference-evidence','start','end','currency','output','actor'):
            p.add_argument('--'+name,required=True)
        p.add_argument('--source');p.add_argument('--metric',choices=['unblended','amortized'],default='unblended')
        p.add_argument('--services',default='');p.add_argument('--synthetic',action='store_true')
    def handle(self,*args,**o):
        from django.conf import settings
        if settings.RUNTIME_ROLE not in ('admin','collector'):
            raise CommandError('Use the isolated administration/collector runtime with authorized customer evidence.')
        customer=Customer.objects.get(pk=o['customer']);source=BillingSource.objects.get(pk=o['source']) if o['source'] else None
        if source and source.customer_id!=customer.pk and not source.shared:raise CommandError('Connection/customer mismatch.')
        try:result=reconcile(customer,Path(o['reference_csv']).read_text(),date.fromisoformat(o['start']),date.fromisoformat(o['end']),o['metric'],o['currency'],source=source,services=o['services'].split(',') if o['services'] else [])
        except ValueError as exc:raise CommandError(str(exc)) from None
        ReconciliationRun.objects.create(customer=customer,source=source,start=o['start'],end=o['end'],metric=o['metric'],currency=o['currency'],scope={k:result[k] for k in ('timezone','services','credits_refunds')},result=result,reference=o['reference_evidence'],synthetic=o['synthetic'],created_by=o['actor'])
        path=Path(o['output']);path.write_text(json.dumps(result,indent=2)+'\n');path.chmod(0o600)
        security_event(o['actor'],'Reconciliation recorded',customer=customer,matched=result['passed'],synthetic=o['synthetic'])
        self.stdout.write('Matched' if result['passed'] else 'Differences or missing evidence; inspect the restricted output file.')
