import json
from django import forms
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, render, redirect
from django.views.decorators.http import require_POST
from django.views.decorators.cache import never_cache
from .models import Customer, CustomerApproval, RolloutReadiness, OperationalAlert, BillingSource
from .web import staff_required
from .authentication import security_event
from .governance import readiness, add_manual_accounts


class ApprovalForm(forms.ModelForm):
    class Meta:
        model = CustomerApproval
        fields = ['contacts','authorized_users','expected_accounts','billing_fields','metadata','storage_region','retention_days','evidence']
        widgets = {field: forms.Textarea(attrs={'rows':3}) for field in ['contacts','authorized_users','expected_accounts','billing_fields','metadata']}
    def clean_expected_accounts(self):
        import re
        values = self.cleaned_data['expected_accounts']
        if not isinstance(values, list) or any(not isinstance(v,str) or not re.fullmatch(r'[0-9]{12}',v) for v in values):
            raise forms.ValidationError('Provide a JSON list of approved 12-digit account IDs.')
        return sorted(set(values))
    def clean(self):
        values = super().clean()
        for name in ('contacts','authorized_users','billing_fields','metadata'):
            if name in values and not isinstance(values[name],list):
                self.add_error(name, 'Provide a JSON list.')
        return values


class RolloutForm(forms.ModelForm):
    class Meta:
        model = RolloutReadiness
        fields = ['owner','planned_date','batch','checklist','security_evidence','independent_review']
        widgets = {'planned_date': forms.DateInput(attrs={'type':'date'}), 'checklist':forms.Textarea(attrs={'rows':4})}
    def clean_checklist(self):
        value = self.cleaned_data['checklist']
        if not isinstance(value,dict):
            raise forms.ValidationError('Provide a JSON object with evidence references.')
        return value


@never_cache
@staff_required
def customer_governance(request, pk):
    customer = get_object_or_404(Customer, pk=pk)
    approval, _ = CustomerApproval.objects.get_or_create(customer=customer)
    rollout, _ = RolloutReadiness.objects.get_or_create(customer=customer)
    form = ApprovalForm(instance=approval)
    rollout_form = RolloutForm(instance=rollout)
    if request.method == 'POST':
        if request.POST.get('action') == 'approval':
            if approval.status == 'approved':
                raise PermissionDenied('Approved records are immutable in the web runtime. An administrator must revoke approval before editing.')
            form = ApprovalForm(request.POST, instance=approval)
            if form.is_valid():
                pending=form.save(commit=False)
                pending.save(update_fields=list(ApprovalForm.Meta.fields)+['updated_at'])
                security_event(request.user.username, 'Approval evidence prepared', customer=customer)
                return redirect('customer_governance', pk=pk)
        elif request.POST.get('action') == 'rollout':
            rollout_form = RolloutForm(request.POST, instance=rollout)
            if rollout_form.is_valid():
                rollout_form.save()
                security_event(request.user.username, 'Rollout plan updated', customer=customer)
                return redirect('customer_governance', pk=pk)
    return render(request, 'billing/governance.html', {'customer':customer,'approval':approval,'form':form,
        'rollout_form':rollout_form, 'readiness':readiness(customer),'active_page':'onboarding'})


@require_POST
@staff_required
def manual_inventory(request, pk):
    source = get_object_or_404(BillingSource, pk=pk)
    from django.contrib import messages
    try:
        add_manual_accounts(source, request.POST.get('accounts','').replace(',',' ').split(), request.user.username)
    except ValueError as exc:
        messages.error(request,str(exc))
    else:
        messages.success(request,'Explicit inventory recorded. Discovery coverage remains labelled separately.')
    return redirect('source_detail',pk=pk)


@never_cache
@login_required
def operations(request):
    from .web import paginate
    alerts = OperationalAlert.objects.all().order_by('-last_seen')
    if request.method == 'POST':
        from .scope import can_edit
        if not can_edit(request.user):
            raise PermissionDenied
        alert = get_object_or_404(alerts, pk=request.POST.get('alert'))
        from django.utils import timezone
        alert.acknowledged_at = timezone.now()
        alert.acknowledged_by = request.user.username
        alert.save()
        security_event(request.user.username,'Operational alert acknowledged',customer=alert.customer,target=str(alert.pk))
        return redirect('operations')
    return render(request,'billing/operations.html',{'page':paginate(request,alerts),'active_page':'activity'})


class InviteForm(forms.Form):
    email=forms.EmailField()
    accounts=forms.CharField(required=False,help_text='Optional comma-separated assigned account IDs.')


@never_cache
@staff_required
def portal_invite(request,pk):
    from .portal import gate,invite
    customer=get_object_or_404(Customer,pk=pk)
    gate(customer)
    form=InviteForm(request.POST or None);token=None
    if request.method=='POST' and form.is_valid():
        token=invite(customer,form.cleaned_data['email'],[x.strip() for x in form.cleaned_data['accounts'].split(',') if x.strip()],request.user.username)
    return render(request,'billing/portal_invite.html',{'form':form,'customer':customer,'token':token})


@never_cache
@login_required
def portal_accept(request):
    from django.conf import settings
    if not settings.EXTERNAL_PORTAL_ENABLED:raise PermissionDenied('External customer access is disabled.')
    # Admission is a controlled capability grant. It is authorized by a high-entropy
    # one-use token and the pre-provisioned authenticated identity's exact email.
    if request.method=='POST':
        from .portal import accept
        from .access import context
        with context(None):
            customer=accept(request.user,request.POST.get('token',''))
        return redirect('customer_detail',pk=customer.pk)
    return render(request,'billing/portal_accept.html')
