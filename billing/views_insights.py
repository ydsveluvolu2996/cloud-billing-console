from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from .insights import executive_insights


@never_cache
@login_required
def insights(request):
    try:
        return render(request, 'billing/insights.html', executive_insights(request.GET))
    except ValueError as exc:
        return render(request, 'billing/filter_error.html', {'error': str(exc), 'active_page': 'insights'}, status=400)
