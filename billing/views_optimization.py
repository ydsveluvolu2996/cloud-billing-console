from django.contrib.auth.decorators import login_required
from django.http import HttpResponseBadRequest
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET
from . import optimization as reporting
from .models import Customer, UserSecurity
from .scope import can_edit


@never_cache
@login_required
@require_GET
def optimization(request):
    try:
        month, currency, metric, selected = reporting.options(request.GET)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    external = UserSecurity.objects.filter(user=request.user, external=True).exists()
    data = reporting.report(month, currency, metric, selected, show_budgets=not external)
    data.update(active_page='optimization', customers=Customer.objects.filter(active=True), can_edit=can_edit(request.user))
    return render(request, 'billing/optimization.html', data)
