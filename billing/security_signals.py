import json
import logging
from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.db.models.signals import post_save
from django.dispatch import receiver
from .models import UserSecurity, AuditEvent, CustomerMembership
from .authentication import security_event
from .access import current_access


@receiver(user_logged_in)
def logged_in(sender, request, user, **kwargs):
    profile, _ = UserSecurity.objects.get_or_create(user=user)
    request.session['security_version'] = profile.session_version
    request.session.pop('mfa_at', None)
    security_event(user.username, 'Primary authentication succeeded', target=str(user.pk))


@receiver(user_login_failed)
def login_failed(sender, credentials, request, **kwargs):
    # Credentials are intentionally not serialized, even when a backend adds keys.
    security_event('unauthenticated', 'Authentication failed', outcome='denied')


@receiver(user_logged_out)
def logged_out(sender, request, user, **kwargs):
    if user:
        security_event(user.username, 'Logout', target=str(user.pk))


@receiver(post_save, sender=AuditEvent)
def protected_audit(sender, instance, created, **kwargs):
    if created:
        # The deployment routes this dedicated logger to CloudWatch with runtime
        # PutLogEvents only. The collector/web cannot delete its central copy.
        logging.getLogger('security.audit').info(json.dumps({'id':instance.pk, 'at':instance.at.isoformat(),
            'actor':instance.actor, 'customer':str(instance.customer_id or ''), 'action':instance.action,
            'details':instance.details}, default=str))


@receiver(post_save, sender=CustomerMembership)
def membership_changed(sender, instance, created, **kwargs):
    from .authentication import revoke_sessions
    access = current_access.get()
    actor = access.username if access else 'administration'
    revoke_sessions(instance.user, actor, 'Customer membership changed')
    security_event(actor, 'Membership changed', customer=instance.customer, target=str(instance.user_id), role=instance.role, active=instance.active)


from .models import AccountAssignment, BillingSource
@receiver(post_save, sender=AccountAssignment)
def ownership_changed(sender,instance,**kwargs):
    from django.db.models import F
    if instance.account.source_id:
        BillingSource.objects.filter(pk=instance.account.source_id).update(ownership_version=F('ownership_version')+1)
