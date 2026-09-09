"""Central scope resolution and authorization.

Every report, export, metadata lookup, saved report and worker query resolves its
customer/source/account/project selection here so a manipulated identifier can never
widen a scope beyond the accounts a customer actually owns.
"""
from dataclasses import dataclass, field
from datetime import date
from uuid import UUID
from django.db.models import Exists, OuterRef, Q
from .models import AccountAssignment, AwsAccount, BillingSource, Cost, Customer, Project

ROLE_READER, ROLE_OPERATOR, ROLE_ADMIN = 'reader', 'operator', 'admin'


def role(user):
    if user.is_superuser:
        return ROLE_ADMIN
    if user.is_staff:
        return ROLE_OPERATOR
    return ROLE_READER


def can_edit(user):
    from django.conf import settings
    from .access import current_access, for_user
    if not settings.ENFORCE_CUSTOMER_AUTHORIZATION:
        return user.is_authenticated and user.is_staff
    access = current_access.get() or for_user(user)
    return bool(user.is_authenticated and (access.portfolio or access.editable))


def parse_uuid(value, message):
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise ValueError(message)


@dataclass
class Scope:
    customer: Customer | None = None
    source: BillingSource | None = None
    account_id: str = ''
    project: Project | None = None
    include_offboarded: bool = True
    errors: list = field(default_factory=list)

    @property
    def customers(self):
        qs = Customer.objects.all()
        if self.customer:
            return qs.filter(pk=self.customer.pk)
        return qs if self.include_offboarded else qs.filter(active=True)

    def costs(self, queryset=None):
        """Cost facts inside this scope; unassigned rows are excluded from customer views."""
        qs = queryset if queryset is not None else Cost.objects.all()
        if self.customer:
            qs = qs.filter(customer=self.customer)
        else:
            qs = qs.filter(customer__isnull=False)
        if self.source:
            qs = qs.filter(source=self.source)
        if self.account_id:
            qs = qs.filter(account_id=self.account_id)
        return qs

    def label(self):
        parts = [self.customer.name if self.customer else 'All customers']
        if self.source:
            parts.append(f'payer {self.source.account_id}')
        if self.account_id:
            parts.append(f'account {self.account_id}')
        if self.project:
            parts.append(f'project {self.project.name}')
        return ' · '.join(parts)


def resolve(params, require_customer=False):
    """Validate customer/source/account/project selections and their containment."""
    scope = Scope()
    cid = params.get('customer', '') or ''
    if cid:
        scope.customer = Customer.objects.filter(pk=parse_uuid(cid, 'Choose a valid customer.')).first()
        if scope.customer is None:
            raise ValueError('This customer could not be found. Choose another customer.')
    elif require_customer:
        raise ValueError('Choose a customer.')
    sid = params.get('source', '') or ''
    if sid:
        scope.source = BillingSource.objects.filter(pk=parse_uuid(sid, 'Choose a valid connection.')).select_related('customer').first()
        if scope.source is None:
            raise ValueError('This connection could not be found.')
        if scope.customer and scope.source.customer_id != scope.customer.pk and not scope.source.shared:
            raise ValueError('This connection does not belong to the selected customer.')
    raw = params.get('account', '') or ''
    account_ids = [a.strip() for a in (raw if isinstance(raw, list) else [raw]) if a and a.strip()]
    for account_id in account_ids:
        if not account_id.isdigit() or len(account_id) != 12:
            raise ValueError('Choose a valid AWS account.')
        if scope.customer and not AccountAssignment.objects.filter(account__account_id=account_id, customer=scope.customer).exists():
            raise ValueError('This account is not part of the selected customer.')
        if scope.source and not (AwsAccount.objects.filter(account_id=account_id, source=scope.source).exists() or Cost.objects.filter(source=scope.source, account_id=account_id).exists()):
            raise ValueError('This account is not part of the selected connection.')
    if len(account_ids) == 1:
        scope.account_id = account_ids[0]
    pid = params.get('project', '') or ''
    if pid:
        try:
            scope.project = Project.objects.select_related('customer').get(pk=int(pid))
        except (ValueError, TypeError, Project.DoesNotExist):
            raise ValueError('Choose a valid project.')
        if scope.customer and scope.project.customer_id != scope.customer.pk:
            raise ValueError('This project belongs to another customer.')
        scope.customer = scope.customer or scope.project.customer
    return scope


def customer_accounts(customer, on=None, source=None):
    """Account IDs assigned to the customer (currently, or on a specific day)."""
    qs = AccountAssignment.objects.filter(customer=customer)
    if on:
        qs = qs.filter(start__lte=on).filter(Q(end__isnull=True) | Q(end__gt=on))
    else:
        qs = qs.filter(end__isnull=True)
    if source:
        qs = qs.filter(account__source=source)
    return sorted(set(qs.values_list('account__account_id', flat=True)))


def source_account_filter(source, customer):
    """Return the LINKED_ACCOUNT restriction for AWS queries made on behalf of ``customer``.

    ``None`` means the customer exclusively owns the connection (no filter required).
    An empty list means the customer owns nothing in this connection.
    """
    if customer is None:
        return None
    from django.conf import settings
    if not settings.ENFORCE_CUSTOMER_AUTHORIZATION and not source.shared and source.customer_id == customer.pk:
        other_owner = AccountAssignment.objects.filter(account__source=source, end__isnull=True).exclude(customer=customer).exists()
        if not other_owner:
            return None
    return customer_accounts(customer, source=source)


def report_units(customer=None, active_only=True):
    """(source, customer, account_filter) tuples describing which AWS queries serve a scope."""
    sources = BillingSource.objects.filter(kind__in=[BillingSource.PAYER, BillingSource.STANDALONE]).select_related('customer')
    if active_only:
        sources = sources.filter(enabled=True, customer__active=True, last_success__isnull=False).exclude(role_arn='')
    units = []
    if customer is None:
        from .access import current_access
        access = current_access.get()
        from django.conf import settings
        if settings.ENFORCE_CUSTOMER_AUTHORIZATION and access and not access.portfolio:
            return [unit for c in Customer.objects.all() for unit in report_units(c, active_only)]
        return [(source, None, None) for source in sources]
    for source in sources:
        if source.customer_id == customer.pk or source.shared or AccountAssignment.objects.filter(account__source=source, customer=customer).exists():
            accounts = source_account_filter(source, customer)
            if accounts is None or accounts:
                units.append((source, customer, accounts))
    return units


def visible_customers(user):
    """Internal team users see every customer; readers see them read-only."""
    from .access import context, for_user
    with context(for_user(user)):
        return Customer.objects.all()


def assert_customer_owns_source(customer, source):
    if source.customer_id != customer.pk and not source.shared:
        raise ValueError('This connection belongs to another customer.')


def unassigned_accounts():
    """Accounts under shared payers (or seen in billing) with no current owner."""
    owned = AccountAssignment.objects.filter(account=OuterRef('pk'), end__isnull=True)
    return AwsAccount.objects.annotate(has_owner=Exists(owned)).filter(has_owner=False).select_related('source', 'source__customer').order_by('payer_account_id', 'account_id')


def ownership_windows(source, customer, start, end):
    """Exclusive-end intervals with a stable set of customer-owned accounts."""
    if customer is None:
        return [(start, end, None)]
    assignments = list(AccountAssignment.objects.filter(customer=customer, account__source=source,
                                                       start__lt=end).filter(Q(end__isnull=True) | Q(end__gt=start)))
    boundaries = sorted({start, end} | {max(start,a.start) for a in assignments} | {min(end,a.end) for a in assignments if a.end})
    result=[]
    from .access import current_access
    access=current_access.get()
    permitted=access.accounts.get(customer.pk) if access else None
    for left,right in zip(boundaries,boundaries[1:]):
        ids=sorted({a.account.account_id for a in assignments if a.covers(left) and (not permitted or a.account.account_id in permitted)})
        if ids:
            result.append((left,right,ids))
    return result
