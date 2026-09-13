"""Deduplicated operational alerts and explicitly gated notification delivery."""
from datetime import timedelta
from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from .models import BillingSource,Job,OperationalAlert,AlertRoute,Alert,AuditEvent


def observe(customer,kind,key,message,severity='warning',now=None):
    now=now or timezone.now()
    event,created=OperationalAlert.objects.get_or_create(dedup_key=key,defaults={'customer':customer,'kind':kind,'message':message,'severity':severity,'last_seen':now,'first_seen':now})
    if not created:
        OperationalAlert.objects.filter(pk=event.pk).update(last_seen=now,count=F('count')+1)
    return event,created


def scan(now=None):
    now=now or timezone.now();count=0
    for source in BillingSource.objects.filter(enabled=True,customer__active=True):
        failures=Job.objects.filter(source=source,status=Job.FAILED,finished_at__gte=now-timedelta(hours=24)).count()
        if failures>=3:
            _,created=observe(source.customer,'collection_failure',f'collection:{source.pk}:{now.date()}',f'{failures} failed jobs in 24 hours for connection {source.account_id}.','critical',now)
            count+=created
        if source.last_success and source.last_success<now-timedelta(hours=12):
            _,created=observe(source.customer,'stale_collection',f'stale:{source.pk}:{now.date()}','No successful collection within 12 hours. Check collector health separately from AWS data availability.',now=now);count+=created
        latest=source.periods.filter(last_success__isnull=False).order_by('-last_day').first()
        if latest and latest.last_day and latest.last_day<now.date()-timedelta(days=2):
            _,created=observe(source.customer,'source_delay',f'source-delay:{source.pk}:{now.date()}','Latest available billing date is older than two days. A successful poll can still contain delayed AWS source data.','info',now);count+=created
        if source.last_error and ('AccessDenied' in source.last_error or 'permission' in source.last_error.lower()):
            _,created=observe(source.customer,'access_failure',f'access:{source.pk}:{source.connection_version}','Customer role access failed. Review trust and approved capability status.','critical',now);count+=created
    for event in AuditEvent.objects.filter(at__gte=now-timedelta(hours=24),action__in=['Membership changed','Role ARN saved; verification queued','External ID rotated']):
        _,created=observe(event.customer,'privilege_change',f'audit:{event.pk}','Membership or connection privileges changed; inspect protected audit evidence.','warning',now);count+=created
    for alert in Alert.objects.filter(triggered_at__gte=now-timedelta(days=1)):
        _,created=observe(alert.budget.customer,'budget_threshold',f'budget:{alert.pk}',alert.message,now=now);count+=created
    from django.db.models import Count
    for entry in AuditEvent.objects.filter(at__gte=now-timedelta(hours=1),action='Export downloaded').values('actor').annotate(n=Count('pk')).filter(n__gte=settings.UNUSUAL_EXPORT_THRESHOLD):
        _,created=observe(None,'unusual_exports',f'exports:{entry["actor"]}:{now.strftime("%Y%m%d%H")}',f'Export volume threshold exceeded by {entry["actor"]}. Review scoped audit records.','warning',now);count+=created
    return count


def deliver(test=False,now=None):
    """Use Django's in-memory backend in tests. Live delivery needs two explicit gates."""
    now=now or timezone.now()
    if test and settings.EMAIL_BACKEND!='django.core.mail.backends.locmem.EmailBackend':
        raise ValueError('Test delivery requires the in-memory email backend.')
    if not test and not (settings.LIVE_NOTIFICATIONS_ENABLED and settings.NOTIFICATION_GATE_REFERENCE):
        return 0
    sent=0
    with transaction.atomic():
        for event in OperationalAlert.objects.select_for_update().filter(acknowledged_at__isnull=True):
            routes=AlertRoute.objects.filter(customer=event.customer,enabled=True,kind__in=['*',event.kind])
            levels={'info':0,'warning':1,'critical':2}
            routes=[r for r in routes if r.recipients and levels.get(event.severity,1)>=levels.get(r.severity,1)]
            if not routes:continue
            escalate=now>=event.first_seen+timedelta(minutes=min(r.escalation_minutes for r in routes))
            if event.delivered_at and (not escalate or event.escalated_at):continue
            recipients=sorted({email for route in routes for email in route.recipients})
            send_mail(('Escalated: ' if escalate else '')+f'Billing alert: {event.kind}',event.message,settings.DEFAULT_FROM_EMAIL,recipients)
            event.delivered_at=now
            if escalate:event.escalated_at=now
            event.save(update_fields=['delivered_at','escalated_at']);sent+=1
    return sent
