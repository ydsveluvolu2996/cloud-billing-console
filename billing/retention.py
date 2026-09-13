"""Explicit, evidence-backed retention; no automatic customer-data deletion."""
from django.db import transaction
from django.utils import timezone
from . import models as m
from .authentication import security_event


def data_sets(customer):
    # Keep identity/ownership tombstones and protected audit evidence. Financial
    # facts are customer-stamped, so shared-payer neighbors remain untouched.
    accounts=set(customer.assignments.values_list('account__account_id',flat=True))
    identities=accounts|{str(customer.pk),customer.name,customer.reference}-{''}
    def contains(value):
        if isinstance(value,dict):return any(contains(x) for x in value.values())
        if isinstance(value,list):return any(contains(x) for x in value)
        return str(value) in identities
    previews=[r.pk for r in m.BulkImport.objects.all() if contains(r.rows)]
    shared_cache_ids=list(m.ExplorerQuery.objects.filter(customer__isnull=True,source_id__in=customer.costs.values('source_id')).values_list('pk',flat=True))
    return {
        'bulk_previews':m.BulkImport.objects.filter(pk__in=previews),
        'aggregate_cache_invalidation':m.ExplorerQuery.objects.filter(pk__in=shared_cache_ids),
        'costs':m.Cost.objects.filter(customer=customer),
        'project_costs':m.ProjectCost.objects.filter(project__customer=customer),
        'saved_reports':m.SavedReport.objects.filter(customer=customer),
        'cached_reports':m.ExplorerQuery.objects.filter(customer=customer),
        'alliance_revisions':m.AllianceRevision.objects.filter(record__customer=customer),
        'alliance_notes':m.AllianceServiceNote.objects.filter(record__customer=customer),
        'alliance_records_and_snapshots':m.AllianceRecord.objects.filter(customer=customer),
        'budget_evaluations':m.BudgetEvaluation.objects.filter(budget__customer=customer),
        'budget_alerts':m.Alert.objects.filter(budget__customer=customer),
        'budget_amounts':m.BudgetAmount.objects.filter(budget__customer=customer),
        'imported_budgets':m.ImportedBudget.objects.filter(source__customer=customer),
        'reconciliation_results':m.ReconciliationRun.objects.filter(customer=customer),
        'invitations':m.PortalInvitation.objects.filter(customer=customer),
    }


def plan(record):
    return {'customer':str(record.customer_id),'database_rows':{key:qs.count() for key,qs in data_sets(record.customer).items()},
        'retention_until':str(record.retention_until),'retained':'Customer/account ownership tombstones, approval references and protected audit records.',
        'external_copies':'Operator must supply evidence for downloaded exports and backup expiry/deletion; this command cannot erase customer-controlled downloads or immutable backups.'}


@transaction.atomic
def purge(record,actor,external_evidence):
    record=m.OffboardingRecord.objects.select_for_update().get(pk=record.pk)
    if record.deletion_completed_at:return plan(record)
    now=timezone.now()
    if record.customer.active or record.customer.sources.filter(enabled=True).exists():
        raise ValueError('Offboard and pause every connection first.')
    if not record.retention_until or record.retention_until>now or record.sessions_expire_after>now:
        raise ValueError('Approved retention and previously issued session windows must expire first.')
    if not all([record.allowlist_removed_at,record.trust_revocation_reference,record.deletion_approved_reference,external_evidence.strip()]):
        raise ValueError('Allowlist removal, trust revocation, deletion approval and export/backup disposition evidence are required.')
    if m.Job.objects.filter(source__customer=record.customer,status__in=[m.Job.QUEUED,m.Job.LEASED]).exists():
        raise ValueError('Resolve remaining jobs before deletion.')
    result=plan(record)
    for qs in data_sets(record.customer).values():qs.delete()
    record.deletion_completed_at=now
    record.notes+='\nExternal export/backup disposition: '+external_evidence
    record.save(update_fields=['deletion_completed_at','notes'])
    security_event(actor,'Retention deletion completed',customer=record.customer,target=str(record.pk),counts=result['database_rows'],evidence=record.deletion_approved_reference,external_evidence=external_evidence)
    return result
