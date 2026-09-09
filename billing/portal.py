"""Disabled-by-default external access using pre-provisioned individual identities."""
import hashlib
import secrets
from datetime import timedelta
from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone
from .models import PortalInvitation,CustomerMembership,UserSecurity
from .governance import readiness
from .authentication import security_event


def gate(customer):
    if not settings.EXTERNAL_PORTAL_ENABLED:
        raise PermissionDenied('External customer access is disabled.')
    if not readiness(customer)['ready']:
        raise PermissionDenied('Security, reconciliation and independent-review readiness evidence is required.')


def invite(customer,email,accounts,actor):
    gate(customer)
    owned=set(customer.assignments.filter(end__isnull=True).values_list('account__account_id',flat=True))
    if not set(accounts).issubset(owned):raise PermissionDenied('Invitation accounts must belong to this customer.')
    token=secrets.token_urlsafe(48)
    row=PortalInvitation.objects.create(customer=customer,email=email,account_ids=accounts,
        token_hash=hashlib.sha256(token.encode()).hexdigest(),created_by=actor,expires_at=timezone.now()+timedelta(hours=24))
    security_event(actor,'Portal invitation prepared',customer=customer,target=str(row.pk))
    return token


@transaction.atomic
def accept(user,token):
    if not settings.EXTERNAL_PORTAL_ENABLED:raise PermissionDenied('External customer access is disabled.')
    from django.db import connection, DatabaseError
    if connection.vendor=='postgresql' and settings.DATABASE_RLS_ENABLED:
        from .models import Customer
        try:
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute('SELECT billing_accept_invitation(%s)',[hashlib.sha256(token.encode()).hexdigest()])
                cid=cursor.fetchone()[0]
        except DatabaseError:
            raise PermissionDenied('Invitation invalid, identity unsuitable, or readiness gates incomplete.') from None
        customer=Customer.objects.get(pk=cid)
        security_event(user.username,'Portal invitation accepted',customer=customer)
        return customer
    row=PortalInvitation.objects.select_for_update().filter(token_hash=hashlib.sha256(token.encode()).hexdigest(),
            accepted_at__isnull=True,revoked_at__isnull=True,expires_at__gt=timezone.now()).first()
    if not row or user.email.lower()!=row.email.lower():raise PermissionDenied('Invitation is invalid, expired or belongs to another identity.')
    gate(row.customer)
    if CustomerMembership.objects.filter(user=user,active=True).exclude(role='customer').exists() or UserSecurity.objects.filter(user=user,portfolio_access=True).exists():
        raise PermissionDenied('Use a separate individual external identity.')
    owned=set(row.customer.assignments.filter(end__isnull=True).values_list('account__account_id',flat=True))
    if not set(row.account_ids).issubset(owned):raise PermissionDenied('Invitation account assignment changed.')
    CustomerMembership.objects.update_or_create(user=user,customer=row.customer,defaults={'role':'customer','active':True,'account_ids':row.account_ids})
    profile,_=UserSecurity.objects.get_or_create(user=user);profile.external=True;profile.save(update_fields=['external'])
    row.accepted_at=timezone.now();row.save(update_fields=['accepted_at'])
    security_event(user.username,'Portal invitation accepted',customer=row.customer,target=str(row.pk))
    return row.customer
