"""Customer consent, rollout and offboarding operations with explicit evidence."""
from datetime import timedelta
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from .models import (CustomerApproval, RolloutReadiness, ReconciliationRun, CustomerMembership,
                     OffboardingRecord, BillingSource, Job, ExplorerQuery, RoleApproval, UserSecurity)
from .authentication import security_event

CHECKS = ('iam_approval','discovery_ownership','user_access','reconciliation','security_prerequisites','rollback_readiness')


def readiness(customer):
    approval = CustomerApproval.objects.filter(customer=customer).first()
    record = RolloutReadiness.objects.filter(customer=customer).first()
    sources = list(BillingSource.objects.filter(customer=customer))
    checks = {
        'iam_approval': bool(approval and approval.status == 'approved' and approval.evidence and sources and all(s.verified_at and s.trust_checks.get('exact_collector_principal') == 'passed' for s in sources)),
        'discovery_ownership': bool(sources and all(s.discovered_at for s in sources) and customer.assignments.filter(end__isnull=True).exists()),
        'user_access': CustomerMembership.objects.filter(customer=customer, active=True).exists(),
        'reconciliation': ReconciliationRun.objects.filter(customer=customer, synthetic=False, result__passed=True).exists(),
        'security_prerequisites': bool(record and record.security_evidence and record.independent_review),
        'rollback_readiness': bool(record and record.checklist.get('rollback_readiness') and record.checklist.get('rollback_evidence')),
    }
    return {'checks': checks, 'ready': all(checks.values()) and bool(record and record.owner and record.planned_date),
            'pending': [k for k,v in checks.items() if not v]}


def stop_customer(customer, actor):
    """Disable publication before cancellation. Existing STS sessions expire separately."""
    with transaction.atomic():
        sources = list(BillingSource.objects.select_for_update().filter(customer=customer))
        for source in sources:
            if source.shared and source.accounts.filter(assignments__end__isnull=True,assignments__customer__active=True).exclude(assignments__customer=customer).exists():
                raise ValueError('Transfer shared-payer custody to an approved active customer before offboarding; other customers still depend on this connection.')
        now = timezone.now()
        for source in sources:
            source.enabled = False
            source.sync_requested = False
            source.connection_version += 1
            source.verified_at = None
            source.save(update_fields=['enabled','sync_requested','connection_version','verified_at'])
        Job.objects.filter(source__in=sources, status__in=[Job.QUEUED,Job.LEASED]).update(status=Job.FAILED, last_error='Cancelled by offboarding', finished_at=now)
        ExplorerQuery.objects.filter(source__in=sources).update(requested=False)
        memberships = list(CustomerMembership.objects.filter(customer=customer, active=True))
        for membership in memberships:
            membership.active = False
            membership.save(update_fields=['active'])
        approval = CustomerApproval.objects.filter(customer=customer).first()
        retention_until = now + timedelta(days=approval.retention_days) if approval and approval.retention_days else None
        record = OffboardingRecord.objects.create(customer=customer, requested_by=actor,
            sessions_expire_after=now + timedelta(hours=1), retention_until=retention_until,
            notes='Collector allowlist removal and customer trust revocation require authorized administration. Previously issued sessions may last up to the previous one-hour maximum; new sessions use 15 minutes. Backups follow separately approved retention.')
        security_event(actor, 'Offboarding gates applied', customer=customer, target=str(record.pk))
        return record


def add_manual_accounts(source, account_ids, actor):
    from .models import AwsAccount
    from .collector import ensure_assignment
    import re
    ids = sorted(set(account_ids))
    if not ids or any(not re.fullmatch(r'[0-9]{12}', value) for value in ids):
        raise ValueError('Enter one or more 12-digit AWS account IDs.')
    approval = CustomerApproval.objects.filter(customer=source.customer).first()
    if not approval or not set(ids).issubset(set(approval.expected_accounts)):
        raise ValueError('Manual account IDs must appear in the customer approval inventory.')
    with transaction.atomic():
        BillingSource.objects.select_for_update().get(pk=source.pk)
        for account_id in ids:
            account, created = AwsAccount.objects.get_or_create(account_id=account_id, defaults={
                'source':source, 'payer_account_id':source.account_id, 'discovery':'manual','state':'UNKNOWN'})
            if account.source_id != source.pk:
                raise ValueError('An account already belongs to another connection; use the reviewed transfer workflow.')
            if not source.shared:
                ensure_assignment(account, source.customer, actor=actor, note='Customer-approved explicit inventory')
        security_event(actor, 'Manual inventory recorded', customer=source.customer, target=str(source.pk), accounts=ids)
    return len(ids)
