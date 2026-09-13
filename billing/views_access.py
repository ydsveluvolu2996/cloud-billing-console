"""A user's effective customer access, without disclosing other identities."""
from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.views.decorators.http import require_GET
from django_otp.plugins.otp_totp.models import TOTPDevice
from .access import for_user
from .models import AccountAssignment, Customer, CustomerMembership
from django.utils import timezone
from django.db.models import Q


@login_required
@require_GET
def my_access(request):
    scope = for_user(request.user)
    memberships = {m.customer_id: m for m in CustomerMembership.objects.filter(user=request.user, active=True).filter(Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()))}
    assignments = AccountAssignment.objects.filter(end__isnull=True).select_related('account')
    accounts = {}
    for assignment in assignments:
        accounts.setdefault(assignment.customer_id, []).append(assignment.account)
    rows = []
    for customer in Customer.objects.filter(active=True).order_by('name'):
        member = memberships.get(customer.pk)
        rows.append({'customer': customer, 'role': 'Administrator' if scope.portfolio else (member.get_role_display() if member else 'Read only'),
                     'restricted': bool(scope.accounts.get(customer.pk)), 'accounts': accounts.get(customer.pk, [])})
    return render(request, 'billing/my_access.html', {'active_page': 'my_access', 'rows': rows, 'portfolio': scope.portfolio,
        'mfa_enrolled': TOTPDevice.objects.filter(user=request.user, confirmed=True).exists()})
