"""Request-bound authorization, applied to every billing model's default manager.

ContextVars isolate concurrent requests. Commands/workers run outside an HTTP context;
production database roles independently constrain those runtimes. Authorization is
resolved afresh on every request, so revocation does not wait for session expiry.
"""
from contextvars import ContextVar
from contextlib import contextmanager
from dataclasses import dataclass, replace
from django.conf import settings
from django.core.exceptions import PermissionDenied, ObjectDoesNotExist
from django.db import models
from django.db.models import Q

current_access = ContextVar('billing_access', default=None)


@dataclass(frozen=True)
class Access:
    user_id: int | None
    username: str
    portfolio: bool
    customers: tuple
    editable: tuple
    accounts: dict
    write: bool = False

    @property
    def ids(self):
        return self.editable if self.write else self.customers


def for_user(user, write=False):
    from .models import CustomerMembership, UserSecurity
    if not user.is_authenticated:
        return Access(None, '', False, (), (), {}, write)
    profile = UserSecurity.objects.filter(user=user).first()
    portfolio = bool(user.is_superuser and profile and profile.portfolio_access)
    from django.utils import timezone
    memberships = list(CustomerMembership.objects.filter(user=user, active=True, customer__active=True).filter(Q(expires_at__isnull=True)|Q(expires_at__gt=timezone.now())))
    if not settings.EXTERNAL_PORTAL_ENABLED:
        memberships = [m for m in memberships if m.role != 'customer']
    return Access(user.pk, user.username, portfolio, tuple(m.customer_id for m in memberships),
                  tuple(m.customer_id for m in memberships if m.role == 'operator'),
                  {m.customer_id: m.account_ids for m in memberships}, write)


@contextmanager
def context(access):
    token = current_access.set(access)
    try:
        yield access
    finally:
        current_access.reset(token)


@contextmanager
def editing():
    access = current_access.get()
    with context(replace(access, write=True) if access else None):
        yield


CUSTOMER_PATHS = {
    'Customer': 'pk', 'BillingSource': 'customer_id', 'Cost': 'customer_id',
    'AccountAssignment': 'customer_id', 'Project': 'customer_id', 'AllocationRule': 'project__customer_id',
    'ProjectCost': 'project__customer_id', 'Budget': 'customer_id', 'BudgetAmount': 'budget__customer_id',
    'BudgetEvaluation': 'budget__customer_id', 'Alert': 'budget__customer_id',
    'AllianceRecord': 'customer_id', 'AllianceServiceNote': 'record__customer_id',
    'CustomerApproval': 'customer_id', 'RolloutReadiness': 'customer_id', 'ReconciliationRun': 'customer_id',
    'OffboardingRecord': 'customer_id', 'OperationalAlert': 'customer_id', 'AlertRoute': 'customer_id',
    'PortalInvitation': 'customer_id', 'SavedReport': 'customer_id', 'AuditEvent': 'customer_id',
    'SyncRun': 'customer_id', 'ExplorerQuery': 'customer_id',
    'RoleApproval': 'source__customer_id', 'CollectionPeriod': 'source__customer_id',
    'Job': 'source__customer_id', 'ImportedBudget': 'source__customer_id',
}
ACCOUNT_PATHS = {'Cost':'account_id', 'AccountAssignment':'account__account_id', 'AllianceRecord':'account__account_id',
                 'AllianceServiceNote':'record__account__account_id', 'ProjectCost':'account_id', 'ImportedBudget':'owning_account_id'}


def restriction(model, access=None):
    access = access or current_access.get()
    if access is None or not settings.ENFORCE_CUSTOMER_AUTHORIZATION:
        return Q()
    if access.portfolio:
        return Q()
    name = model.__name__
    if name == 'BulkImport':
        return Q(uploaded_by=access.username) if access.editable else Q(pk__in=[])
    if name == 'BillingSource' and not access.write:
        from .models import AccountAssignment
        owned_sources=AccountAssignment.objects.filter(customer_id__in=access.ids).values_list('account__source_id',flat=True)
        return Q(customer_id__in=[cid for cid in access.ids if not access.accounts.get(cid)]) | Q(pk__in=owned_sources)
    if name == 'AwsAccount':
        q = Q(assignments__customer_id__in=access.ids)
        restricted = Q(pk__in=[])
        for cid in access.ids:
            part = Q(assignments__customer_id=cid)
            ids = access.accounts.get(cid)
            restricted |= part & Q(account_id__in=ids) if ids else part
        return q & restricted
    path = CUSTOMER_PATHS.get(name)
    if not path:
        return Q(pk__in=[])
    q = Q(pk__in=[])
    for cid in access.ids:
        part = Q(**{path: cid})
        accounts = access.accounts.get(cid)
        if accounts:
            account_path = ACCOUNT_PATHS.get(name)
            if account_path:
                part &= Q(**{account_path + '__in': accounts})
            elif name not in ('Customer', 'AuditEvent'):
                # Never expose precomputed whole-customer caches, project/budget
                # totals or connection metadata to an account-restricted identity.
                continue
            if name == 'AuditEvent':
                continue
        q |= part
    if name == 'ExplorerQuery':
        q &= Q(requested_by_id=access.user_id)
    if name == 'SavedReport':
        q &= Q(created_by=access.username)
    return q


class ScopedQuerySet(models.QuerySet):
    def update(self, **kwargs):
        self._validate_changes(kwargs)
        return super().update(**kwargs)

    def _validate_changes(self, kwargs):
        access = current_access.get()
        if access is None or not settings.ENFORCE_CUSTOMER_AUTHORIZATION:
            return
        if self.model.__name__ in ('RoleApproval', 'CustomerApproval') and any(k in kwargs for k in ('status','approved_by','approved_at','evidence')):
            raise PermissionDenied('Approval changes require the administration command and evidence.')
        if self.model.__name__ == 'AuditEvent':
            raise PermissionDenied('Audit records are append-only.')
        # Scope-changing updates must use checked model saves. SQL expressions and
        # bulk foreign-key changes could otherwise bypass containment validation.
        if any(k in kwargs for k in ('customer','customer_id','source','source_id','project','project_id','budget','budget_id','record','record_id','account','account_id')):
            raise PermissionDenied('Use the validated ownership workflow to change object scope.')

    def delete(self):
        if current_access.get() and self.model.__name__ == 'AuditEvent':
            raise PermissionDenied('Audit records are append-only.')
        return super().delete()

    def bulk_create(self, objs, **kwargs):
        for obj in objs:
            validate_object(obj)
        return super().bulk_create(objs, **kwargs)

    def bulk_update(self, objs, fields, **kwargs):
        self._validate_changes(dict.fromkeys(fields))
        for obj in objs:
            validate_object(obj)
        return super().bulk_update(objs, fields, **kwargs)


class ScopedManager(models.Manager.from_queryset(ScopedQuerySet)):
    def get_queryset(self):
        qs = super().get_queryset().filter(restriction(self.model))
        return qs.distinct() if self.model.__name__ == 'AwsAccount' and current_access.get() else qs


def validate_object(obj):
    access = current_access.get()
    if access is None or not settings.ENFORCE_CUSTOMER_AUTHORIZATION:
        return
    if obj.__class__.__name__ in ('CustomerApproval','RoleApproval') and getattr(obj, 'status', 'pending') not in ('pending','requested'):
        raise PermissionDenied('Only the administration runtime can approve roles or customer consent.')
    if obj.__class__.__name__ == 'AuditEvent' and not obj._state.adding:
        raise PermissionDenied('Audit records are append-only.')
    if access.portfolio:
        return
    name = obj.__class__.__name__
    if name == 'BulkImport':
        if obj.uploaded_by != access.username or not access.editable:
            raise PermissionDenied('This import belongs to another operator.')
        return
    if name == 'Customer':
        if obj.pk not in access.ids:
            raise PermissionDenied('Customer creation requires explicit portfolio administration.')
        return
    path = CUSTOMER_PATHS.get(name)
    if not path:
        raise PermissionDenied('No authorized scope for this object.')
    if name == 'AuditEvent' and obj.customer_id is None:
        if obj.actor != access.username:
            raise PermissionDenied('Invalid audit actor.')
        return
    # Follow model relations using explicit containment; the base manager used by
    # Django relations must not become an authorization bypass.
    current = obj
    try:
        for key in path.split('__'):
            current = getattr(current, key)
    except (AttributeError, ObjectDoesNotExist):
        raise PermissionDenied('A customer scope is required.') from None
    if current not in access.ids:
        raise PermissionDenied('The object is outside your authorized customer scope.')
    for field in obj._meta.fields:
        if field.is_relation and field.related_model._meta.app_label == 'billing' and getattr(obj, field.attname, None):
            related = field.related_model
            if not related.objects.filter(pk=getattr(obj, field.attname)).exists():
                raise PermissionDenied('A related object is outside your authorized scope.')
    if access.accounts.get(current):
        path = ACCOUNT_PATHS.get(name)
        if not path:
            raise PermissionDenied('This operation needs whole-customer authorization.')
        value = obj
        for key in path.split('__'):
            value = getattr(value, key)
        if value not in access.accounts[current]:
            raise PermissionDenied('Account scope denied.')
