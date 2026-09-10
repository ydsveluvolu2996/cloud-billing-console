"""MFA-verified portfolio administrators manage identities through one capability."""
import json
import logging
import re
from functools import wraps
from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.hashers import make_password
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction, DatabaseError
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from billing.access import for_user, context
from billing.authentication import mfa_current, security_event
from billing.models import Customer, CustomerMembership, UserSecurity, AccountAssignment


def administrator_required(view):
    @wraps(view)
    @login_required
    def wrapped(request, *args, **kwargs):
        if not for_user(request.user).portfolio or not mfa_current(request):
            raise PermissionDenied('A verified administrator must perform this action.')
        return view(request, *args, **kwargs)
    return wrapped


class UserForm(forms.Form):
    username = forms.CharField(max_length=150, validators=User._meta.get_field('username').validators)
    first_name = forms.CharField(max_length=150, required=False)
    last_name = forms.CharField(max_length=150, required=False)
    email = forms.EmailField(required=False)
    role = forms.ChoiceField(choices=[('viewer','Viewer — read selected customers'),('operator','Operator — manage selected customers'),('admin','Administrator — all customers and users')])
    customers = forms.ModelMultipleChoiceField(queryset=Customer.objects.none(), required=False,
        widget=forms.SelectMultiple(attrs={'size':8}), help_text='Choose customers for a viewer or operator. Administrators can access all current and future customers.')
    account_ids = forms.CharField(required=False, widget=forms.Textarea(attrs={'rows':2}),
        label='Limit to AWS account IDs', help_text='Optional comma-separated 12-digit IDs. Leave empty for all accounts in each selected customer.')
    active = forms.BooleanField(required=False, initial=True)
    password1 = forms.CharField(required=False, strip=False, label='Password', widget=forms.PasswordInput(attrs={'autocomplete':'new-password'}))
    password2 = forms.CharField(required=False, strip=False, label='Confirm password', widget=forms.PasswordInput(attrs={'autocomplete':'new-password'}))

    def __init__(self, *args, target=None, actor=None, **kwargs):
        self.target, self.actor = target, actor
        if target and 'initial' not in kwargs:
            profile=UserSecurity.objects.filter(user=target).first()
            memberships=list(CustomerMembership.objects.filter(user=target,active=True))
            role='admin' if target.is_superuser and profile and profile.portfolio_access else ('operator' if any(m.role=='operator' for m in memberships) else 'viewer')
            kwargs['initial']={name:getattr(target,name) for name in ('username','first_name','last_name','email')}
            kwargs['initial'].update(role=role,active=target.is_active,customers=[] if role=='admin' else [m.customer_id for m in memberships],account_ids='' if role=='admin' else ', '.join(a for m in memberships for a in m.account_ids))
        super().__init__(*args,**kwargs)
        self.fields['customers'].queryset=Customer.objects.filter(active=True).order_by('name')
        self.fields['username'].disabled=bool(target)
        self.fields['password1'].required=self.fields['password2'].required=not target
        self.fields['password1'].help_text='At least 12 characters. The user will enroll their own authenticator on first sign-in.' if not target else 'Leave both password fields empty to keep the current password. A change signs the user out.'

    def clean_username(self):
        value=self.cleaned_data['username']
        if not self.target and User.objects.filter(username__iexact=value).exists():
            raise forms.ValidationError('This username is already in use.')
        return value

    def clean(self):
        data=super().clean()
        password=data.get('password1','')
        if password!=data.get('password2',''):
            self.add_error('password2','Passwords do not match.')
        if password:
            candidate=User(username=data.get('username',''),email=data.get('email',''),first_name=data.get('first_name',''),last_name=data.get('last_name',''))
            try:validate_password(password,candidate)
            except ValidationError as exc:self.add_error('password1',exc)
        if self.target and self.actor and self.target.pk==self.actor.pk and (not data.get('active') or data.get('role')!='admin'):
            raise forms.ValidationError('You cannot remove your own administrator access.')
        ids=sorted(set(re.split(r'[\s,]+',data.get('account_ids','').strip()))-{''})
        if any(not re.fullmatch(r'[0-9]{12}',a) for a in ids):
            self.add_error('account_ids','Use only 12-digit AWS account IDs.')
            return data
        customers=list(data.get('customers',[]));grants=[]
        if data.get('role')=='admin':
            if ids or customers:raise forms.ValidationError('Administrators already have all customer access. Clear the customer and account selections.')
        else:
            owned={str(c.pk):set(AccountAssignment.objects.filter(customer=c,end__isnull=True).values_list('account__account_id',flat=True)) for c in customers}
            if ids and (not set(ids).issubset(set().union(*owned.values())) or any(not set(ids)&values for values in owned.values())):
                self.add_error('account_ids','Every ID must belong to a selected customer, and every selected customer needs at least one ID when restricting accounts.')
            grants=[{'customer_id':str(c.pk),'role':data.get('role'),'account_ids':sorted(set(ids)&owned[str(c.pk)]) if ids else []} for c in customers]
        data['grants']=grants
        return data


def apply_user_change(actor, command):
    if not for_user(actor).portfolio:
        raise PermissionDenied('Portfolio administrator required.')
    with transaction.atomic():
        if connection.vendor=='postgresql' and settings.DATABASE_RLS_ENABLED:
            with connection.cursor() as cursor:
                cursor.execute('SELECT billing_admin_user(%s::jsonb)',[json.dumps(command)])
                event=cursor.fetchone()[0]
            logging.getLogger('security.audit').info(json.dumps(event,default=str))
            return event['user_id']
        # SQLite is used only in isolated development tests. Production calls the
        # guarded database function, without granting broad identity writes.
        target=User.objects.select_for_update().get(pk=command['id']) if command.get('id') else None
        if target and target.pk==actor.pk and (command['action']=='delete' or not command['active'] or command['role']!='admin'):
            raise ValidationError('You cannot remove your own administrator access.')
        username=target.username if target else command['username']
        if command['action']=='delete':
            from django.contrib.admin.models import LogEntry
            from billing.models import PortalInvitation
            if LogEntry.objects.filter(user=target).exists():raise ValidationError('Disable this user to preserve historical administration records.')
            with context(None):PortalInvitation.objects.filter(target_user=target).update(target_user=None,revoked_at=timezone.now())
            CustomerMembership.objects.filter(user=target).delete()
            identifier=target.pk;target.delete()
        else:
            target=target or User(username=username)
            for name in ('email','first_name','last_name'):setattr(target,name,command[name])
            target.is_active=command['active'];target.is_superuser=target.is_staff=command['role']=='admin'
            if command['password']:target.password=command['password']
            target.save();identifier=target.pk
            profile,_=UserSecurity.objects.get_or_create(user=target)
            profile.portfolio_access=command['role']=='admin';profile.external=False;profile.session_version+=1;profile.save()
            CustomerMembership.objects.filter(user=target).update(active=False)
            for grant in command['grants']:
                CustomerMembership.objects.update_or_create(user=target,customer_id=grant['customer_id'],defaults={'role':grant['role'],'account_ids':grant['account_ids'],'active':True,'expires_at':None,'support_reason':''})
        security_event(actor.username,'User '+command['action'],target=str(identifier),username=username,role=command.get('role'),customer_access=command.get('grants',[]))
        return identifier


@never_cache
@administrator_required
def users(request):
    rows=User.objects.select_related('security').prefetch_related('customer_memberships__customer','totpdevice_set').order_by('username')
    q=request.GET.get('q','').strip()
    if q:rows=rows.filter(Q(username__icontains=q)|Q(email__icontains=q))
    return render(request,'billing/users.html',{'users':rows,'active_page':'users','search':q})


@never_cache
@sensitive_post_parameters()
@administrator_required
def user_edit(request, pk=None):
    target=get_object_or_404(User,pk=pk) if pk else None
    form=UserForm(request.POST or None,target=target,actor=request.user)
    delete_error=''
    if request.method=='POST':
        deleting=request.POST.get('action')=='delete'
        if deleting and target:
            form=UserForm(target=target,actor=request.user)
            if request.POST.get('confirm_username')!=target.username:
                delete_error='Type the exact username to confirm deletion.'
            else:
                try:
                    apply_user_change(request.user,{'action':'delete','id':target.pk})
                except (ValidationError,DatabaseError) as exc:
                    delete_error='User deletion was refused. You cannot delete yourself or an account with historical administration records.'
                else:
                    messages.success(request,'User deleted. Billing records and billing audit history were retained.')
                    return redirect('users')
        elif form.is_valid():
            data=form.cleaned_data
            command={name:data[name] for name in ('username','email','first_name','last_name','role','active','grants')}
            command.update(action='update' if target else 'create',id=target.pk if target else None,
                           password=make_password(data['password1'],hasher='pbkdf2_sha256') if data.get('password1') else '')
            try:apply_user_change(request.user,command)
            except (ValidationError,DatabaseError):form.add_error(None,'The user could not be saved. Check the username and customer/account assignments and try again.')
            else:
                messages.success(request,'User saved. Access changes take effect immediately; existing sessions are revoked.')
                return redirect('users')
    return render(request,'billing/user_edit.html',{'form':form,'target':target,'active_page':'users','delete_error':delete_error})
